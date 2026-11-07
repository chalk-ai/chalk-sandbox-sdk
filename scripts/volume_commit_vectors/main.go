// Independently project protobuf wire fixtures and check deterministic CBOR.
// This offline checker does not submit commits or validate uploaded objects.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
	"unicode"

	volume "github.com/chalk-ai/chalk-go/gen/chalk/volume/v2"
	"github.com/fxamacker/cbor/v2"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

const maxBytes = 4 * 1024 * 1024

var canonicalVolumeUUID = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// Pin tag/name, kind, cardinality, presence, oneof and value type.
var schemas = map[string]string{
	"CommitIntent":            "1:volume:11:1:1:-:.chalk.volume.v2.VolumeRef 2:commit_id:9:1:0:-:- 3:ref:9:1:1:_ref:- 4:base_version_id:4:1:1:_base_version_id:- 5:base_sequence_number:4:1:1:_base_sequence_number:- 6:uploaded_object_references:11:3:0:-:.chalk.volume.v2.UploadedObjectReference 7:author:9:1:0:-:- 8:path_deltas:11:1:1:deltas:.chalk.volume.v2.PathDeltaList 9:inode_deltas:11:1:1:deltas:.chalk.volume.v2.InodeDeltaList 10:uploaded_deltas:11:1:1:deltas:.chalk.volume.v2.UploadedObjectReference",
	"VolumeRef":               "1:volume_id:9:1:0:-:- 2:name:9:1:0:-:-",
	"PathDeltaList":           "1:upserts:11:3:0:-:.chalk.volume.v2.PathFileDelta 2:removes:11:3:0:-:.chalk.volume.v2.PathRemoveDelta",
	"PathFileDelta":           "1:path:9:1:0:-:- 2:node:11:1:1:-:.chalk.volume.v2.FileNode 3:mode:14:1:0:-:.chalk.volume.v2.PathWriteMode",
	"FileNode":                "1:metadata:11:1:1:-:.chalk.volume.v2.FileMetadata 2:file:11:1:1:node:.chalk.volume.v2.RegularFileNode 3:directory:11:1:1:node:.chalk.volume.v2.DirectoryNode 4:symlink:11:1:1:node:.chalk.volume.v2.SymlinkNode",
	"RegularFileNode":         "1:content:11:1:1:-:.chalk.volume.v2.ContentRef",
	"FileMetadata":            "1:mode:13:1:1:_mode:- 2:updated_at:11:1:1:_updated_at:.google.protobuf.Timestamp 3:uid:13:1:1:_uid:- 4:gid:13:1:1:_gid:-",
	"ContentRef":              "1:empty:11:1:1:content:.chalk.volume.v2.EmptyFileContent 2:inline:11:1:1:content:.chalk.volume.v2.InlineFileContent 3:chunked:11:1:1:content:.chalk.volume.v2.ChunkedContentRef 4:packed:11:1:1:content:.chalk.volume.v2.PackedContentRef",
	"EmptyFileContent":        "",
	"InlineFileContent":       "1:data:12:1:0:-:- 2:hash:9:1:0:-:- 3:size:4:1:0:-:-",
	"ChunkedContentRef":       "1:hash:9:1:0:-:- 2:size:4:1:0:-:- 3:chunks:11:3:0:-:.chalk.volume.v2.ChunkRef",
	"PackedContentRef":        "1:hash:9:1:0:-:- 2:size:4:1:0:-:- 3:pack:11:1:1:-:.chalk.volume.v2.PackEntryRef",
	"ChunkRef":                "1:object_key:9:1:0:-:- 2:hash:9:1:0:-:- 3:size:4:1:0:-:- 4:offset:4:1:0:-:-",
	"PackEntryRef":            "1:object_key:9:1:0:-:- 2:pack_id:9:1:0:-:- 3:offset:4:1:0:-:- 4:size:4:1:0:-:-",
	"UploadedObjectReference": "1:object_key:9:1:0:-:- 2:hash:9:1:0:-:- 3:content_size:4:1:0:-:- 4:kind:14:1:0:-:.chalk.volume.v2.UploadedObjectKind",
	"PathRemoveDelta":         "1:path:9:1:0:-:- 2:recursive:8:1:0:-:-",
}

type vector struct {
	Name          string `json:"name"`
	EnvironmentID string `json:"environment_id"`
	VolumeID      string `json:"volume_id"`
	WireHex       string `json:"wire_hex"`
	CanonicalHex  string `json:"canonical_hex"`
	SHA256        string `json:"sha256"`
	Reject        bool   `json:"reject"`
}

type projector struct{ volumeID string }

func field(m protoreflect.Message, n int) protoreflect.FieldDescriptor {
	return m.Descriptor().Fields().ByNumber(protoreflect.FieldNumber(n))
}

func text(m protoreflect.Message, n int) string { return m.Get(field(m, n)).String() }

func required(m protoreflect.Message, n int) error {
	if !m.Has(field(m, n)) {
		return fmt.Errorf("required field absent: %s/%d", m.Descriptor().Name(), n)
	}
	return nil
}

func (p projector) objectKey(key, keyspace string) (string, error) {
	key = strings.Trim(key, "/")
	if key == "" {
		return "", fmt.Errorf("empty object key")
	}
	for _, part := range strings.Split(key, "/") {
		if part == "" || part == "." || part == ".." {
			return "", fmt.Errorf("invalid object key")
		}
	}
	prefix := "volumes/" + p.volumeID + "/" + keyspace + "/"
	if strings.HasPrefix(key, prefix) {
		return key, nil
	}
	if strings.HasPrefix(key, "volumes/") {
		return "", fmt.Errorf("different volume/keyspace")
	}
	return prefix + key, nil
}

func (p projector) message(m protoreflect.Message) (map[uint64]any, error) {
	name := string(m.Descriptor().Name())
	schema, ok := schemas[name]
	if !ok || len(m.GetUnknown()) != 0 {
		return nil, fmt.Errorf("unsupported/unknown message: %s", name)
	}
	tags := strings.Fields(schema)
	if len(tags) != m.Descriptor().Fields().Len() {
		return nil, fmt.Errorf("uncovered schema: %s", name)
	}
	for _, tag := range tags {
		parts := strings.Split(tag, ":")
		n, err := strconv.Atoi(parts[0])
		if err != nil {
			return nil, err
		}
		fd := field(m, n)
		if fd == nil {
			return nil, fmt.Errorf("uncovered tag: %s", tag)
		}
		oneof := "-"
		if fd.ContainingOneof() != nil {
			oneof = string(fd.ContainingOneof().Name())
		}
		valueType := "-"
		if fd.Message() != nil {
			valueType = "." + string(fd.Message().FullName())
		}
		if fd.Enum() != nil {
			valueType = "." + string(fd.Enum().FullName())
		}
		presence := 0
		if fd.HasPresence() {
			presence = 1
		}
		signature := fmt.Sprintf("%d:%s:%d:%d:%d:%s:%s", fd.Number(), fd.Name(), fd.Kind(), fd.Cardinality(), presence, oneof, valueType)
		if signature != tag {
			return nil, fmt.Errorf("uncovered descriptor schema: %s", tag)
		}
	}

	switch name {
	case "VolumeRef":
		if text(m, 1) != "" && text(m, 1) != p.volumeID {
			return nil, fmt.Errorf("volume differs")
		}
		if text(m, 1) == "" && text(m, 2) == "" {
			return nil, fmt.Errorf("missing selector")
		}
		return map[uint64]any{1: p.volumeID}, nil
	case "CommitIntent":
		if err := required(m, 1); err != nil {
			return nil, err
		}
		if text(m, 2) == "" || !m.Has(field(m, 8)) {
			return nil, fmt.Errorf("missing ID/path deltas")
		}
	case "PathFileDelta":
		if err := required(m, 2); err != nil {
			return nil, err
		}
		mode := m.Get(field(m, 3)).Enum()
		if mode != 0 && mode != 1 {
			return nil, fmt.Errorf("unsupported write mode")
		}
	case "FileNode":
		if !m.Has(field(m, 2)) {
			return nil, fmt.Errorf("regular file arm required")
		}
	case "RegularFileNode":
		if err := required(m, 1); err != nil {
			return nil, err
		}
	case "FileMetadata":
		for _, n := range []int{2, 3, 4} {
			if m.Has(field(m, n)) {
				return nil, fmt.Errorf("unsupported metadata")
			}
		}
	case "ContentRef":
		if m.WhichOneof(m.Descriptor().Oneofs().Get(0)) == nil {
			return nil, fmt.Errorf("content arm absent")
		}
	case "PackedContentRef":
		if err := required(m, 3); err != nil {
			return nil, err
		}
	case "UploadedObjectReference":
		kind := m.Get(field(m, 4)).Enum()
		if kind != 1 && kind != 2 {
			return nil, fmt.Errorf("unsupported object kind")
		}
		if m.Get(field(m, 3)).Uint() > 1<<63-1 {
			return nil, fmt.Errorf("object size overflow")
		}
	}
	result := make(map[uint64]any)
	for i := 0; i < m.Descriptor().Fields().Len(); i++ {
		fd := m.Descriptor().Fields().Get(i)
		if fd.HasPresence() && !m.Has(fd) {
			continue
		}
		v := m.Get(fd)
		if fd.IsList() {
			items := make([]any, 0, v.List().Len())
			for j := 0; j < v.List().Len(); j++ {
				item, err := p.value(fd, v.List().Get(j))
				if err != nil {
					return nil, err
				}
				items = append(items, item)
			}
			result[uint64(fd.Number())] = items
		} else {
			item, err := p.value(fd, v)
			if err != nil {
				return nil, err
			}
			result[uint64(fd.Number())] = item
		}
	}
	switch name {
	case "CommitIntent":
		ref := text(m, 3)
		if ref == "" {
			ref = "main"
		}
		result[3] = ref
	case "PathFileDelta":
		result[3] = uint64(1)
	case "ChunkRef", "PackEntryRef", "UploadedObjectReference":
		space := "chunks"
		if name == "PackEntryRef" || name == "UploadedObjectReference" && m.Get(field(m, 4)).Enum() == 2 {
			space = "packs"
		}
		key, err := p.objectKey(text(m, 1), space)
		if err != nil {
			return nil, err
		}
		result[1] = key
	}
	return result, nil
}

func (p projector) value(fd protoreflect.FieldDescriptor, v protoreflect.Value) (any, error) {
	switch fd.Kind() {
	case protoreflect.MessageKind:
		return p.message(v.Message())
	case protoreflect.StringKind:
		return v.String(), nil
	case protoreflect.BytesKind:
		return append([]byte{}, v.Bytes()...), nil
	case protoreflect.BoolKind:
		return v.Bool(), nil
	case protoreflect.Uint32Kind, protoreflect.Uint64Kind:
		return v.Uint(), nil
	case protoreflect.EnumKind:
		n := v.Enum()
		if n < 0 || fd.Enum().Values().ByNumber(n) == nil {
			return nil, fmt.Errorf("unknown enum")
		}
		return uint64(n), nil
	default:
		return nil, fmt.Errorf("unsupported value kind")
	}
}

func canonical(v vector) ([]byte, error) {
	wire, err := hex.DecodeString(v.WireHex)
	if err != nil {
		return nil, err
	}
	if len(wire) > maxBytes {
		return nil, fmt.Errorf("wire bound")
	}
	if v.EnvironmentID == "" || v.EnvironmentID == "." || v.EnvironmentID == ".." || strings.Contains(v.EnvironmentID, "/") || strings.ContainsFunc(v.EnvironmentID, func(r rune) bool { return unicode.IsSpace(r) || r < 32 || r == 127 }) {
		return nil, fmt.Errorf("authenticated environment required")
	}
	parts := strings.Split(v.VolumeID, "/")
	if len(parts) != 2 || parts[0] != v.EnvironmentID || !canonicalVolumeUUID.MatchString(parts[1]) {
		return nil, fmt.Errorf("environment/canonical-UUID volume identity required")
	}
	intent := new(volume.CommitIntent)
	if err := proto.Unmarshal(wire, intent); err != nil {
		return nil, err
	}
	projection, err := (projector{v.VolumeID}).message(intent.ProtoReflect())
	if err != nil {
		return nil, err
	}
	encoder, err := cbor.CoreDetEncOptions().EncMode()
	if err != nil {
		return nil, err
	}
	data, err := encoder.Marshal(map[uint64]any{0: "chalk.volume.commit-request", 1: uint64(1), 2: v.EnvironmentID, 3: projection})
	if len(data) > maxBytes {
		return nil, fmt.Errorf("canonical bound")
	}
	return data, err
}

func run() error {
	if len(os.Args) != 2 {
		return fmt.Errorf("usage: checker vectors.json")
	}
	input, err := os.Open(os.Args[1])
	if err != nil {
		return err
	}
	defer input.Close()
	var vectors []vector
	decoder := json.NewDecoder(input)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&vectors); err != nil {
		return err
	}
	accepted, rejected := 0, 0
	for _, v := range vectors {
		data, err := canonical(v)
		if v.Reject {
			if err == nil {
				return fmt.Errorf("%s: accepted rejected vector", v.Name)
			}
			rejected++
			continue
		}
		if err != nil {
			return fmt.Errorf("%s: %w", v.Name, err)
		}
		digest := sha256.Sum256(data)
		if hex.EncodeToString(data) != v.CanonicalHex || hex.EncodeToString(digest[:]) != v.SHA256 {
			return fmt.Errorf("%s: canonical bytes/digest differs", v.Name)
		}
		accepted++
	}
	return json.NewEncoder(os.Stdout).Encode(map[string]any{"accepted": accepted, "rejected": rejected, "all_vectors_match": true, "rpc_calls": 0})
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
