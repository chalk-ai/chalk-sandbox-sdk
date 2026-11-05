"""Private format1 request projection; no RPC or server verification claim.

The protocol's separate verified RPC and durable backend proof are required
before this digest can establish which request a commit ID accepted. This
module encodes identity only; it does not verify uploaded bytes/content hashes.
"""

from __future__ import annotations

import hashlib
from typing import Any, TypeAlias

from protobuf import Oneof

from chalkcompute._gen.chalk.volume.v2 import volume_pb as pb

_MAX_BYTES = 4 * 1024 * 1024
_Value: TypeAlias = int | bool | str | bytes | list["_Value"] | dict[int, "_Value"]

# Pin field tag/name, wire kind, cardinality, presence, oneof and value type.
# Same-name schema edits require explicit format review, not just slot coverage.
_SCHEMAS = {
    "CommitIntent": "1:volume:11:1:1:-:.chalk.volume.v2.VolumeRef 2:commit_id:9:1:0:-:- 3:ref:9:1:1:_ref:- 4:base_version_id:4:1:1:_base_version_id:- 5:base_sequence_number:4:1:1:_base_sequence_number:- 6:uploaded_object_references:11:3:0:-:.chalk.volume.v2.UploadedObjectReference 7:author:9:1:0:-:- 8:path_deltas:11:1:1:deltas:.chalk.volume.v2.PathDeltaList 9:inode_deltas:11:1:1:deltas:.chalk.volume.v2.InodeDeltaList 10:uploaded_deltas:11:1:1:deltas:.chalk.volume.v2.UploadedObjectReference",
    "VolumeRef": "1:volume_id:9:1:0:-:- 2:name:9:1:0:-:-",
    "PathDeltaList": "1:upserts:11:3:0:-:.chalk.volume.v2.PathFileDelta 2:removes:11:3:0:-:.chalk.volume.v2.PathRemoveDelta",
    "PathFileDelta": "1:path:9:1:0:-:- 2:node:11:1:1:-:.chalk.volume.v2.FileNode 3:mode:14:1:0:-:.chalk.volume.v2.PathWriteMode",
    "FileNode": "1:metadata:11:1:1:-:.chalk.volume.v2.FileMetadata 2:file:11:1:1:node:.chalk.volume.v2.RegularFileNode 3:directory:11:1:1:node:.chalk.volume.v2.DirectoryNode 4:symlink:11:1:1:node:.chalk.volume.v2.SymlinkNode",
    "RegularFileNode": "1:content:11:1:1:-:.chalk.volume.v2.ContentRef",
    "FileMetadata": "1:mode:13:1:1:_mode:- 2:updated_at:11:1:1:_updated_at:.google.protobuf.Timestamp 3:uid:13:1:1:_uid:- 4:gid:13:1:1:_gid:-",
    "ContentRef": "1:empty:11:1:1:content:.chalk.volume.v2.EmptyFileContent 2:inline:11:1:1:content:.chalk.volume.v2.InlineFileContent 3:chunked:11:1:1:content:.chalk.volume.v2.ChunkedContentRef 4:packed:11:1:1:content:.chalk.volume.v2.PackedContentRef",
    "EmptyFileContent": "",
    "InlineFileContent": "1:data:12:1:0:-:- 2:hash:9:1:0:-:- 3:size:4:1:0:-:-",
    "ChunkedContentRef": "1:hash:9:1:0:-:- 2:size:4:1:0:-:- 3:chunks:11:3:0:-:.chalk.volume.v2.ChunkRef",
    "PackedContentRef": "1:hash:9:1:0:-:- 2:size:4:1:0:-:- 3:pack:11:1:1:-:.chalk.volume.v2.PackEntryRef",
    "ChunkRef": "1:object_key:9:1:0:-:- 2:hash:9:1:0:-:- 3:size:4:1:0:-:- 4:offset:4:1:0:-:-",
    "PackEntryRef": "1:object_key:9:1:0:-:- 2:pack_id:9:1:0:-:- 3:offset:4:1:0:-:- 4:size:4:1:0:-:-",
    "UploadedObjectReference": "1:object_key:9:1:0:-:- 2:hash:9:1:0:-:- 3:content_size:4:1:0:-:- 4:kind:14:1:0:-:.chalk.volume.v2.UploadedObjectKind",
    "PathRemoveDelta": "1:path:9:1:0:-:- 2:recursive:8:1:0:-:-",
}


def canonical_commit_request(
    intent: pb.CommitIntent, *, environment_id: str, volume_id: str
) -> bytes:
    """Encode the complete supported identity, rejecting unknown semantics.

    ``volume_id`` must be the independently observed/resolved stable ID in the
    authenticated ``environment_id``. Resolving those identities and validating
    actual content are backend responsibilities, not established by this call.
    Path strings are preserved verbatim; callers must apply the backend's path
    validation before submission. A digest does not establish path validity.
    """
    _check(
        intent,
        "volume commit_id ref base_version_id base_sequence_number uploaded_object_references author deltas",
    )
    if len(intent.to_binary()) > _MAX_BYTES:
        raise ValueError("Commit request exceeds4MiB wire bound")
    if (
        not isinstance(environment_id, str)
        or not isinstance(volume_id, str)
        or not environment_id
        or not volume_id
        or "/" in volume_id
        or volume_id in {".", ".."}
    ):
        raise ValueError("Require authenticated environment and stable volume identity")
    if intent.volume is None:
        raise ValueError("Commit intent requires a volume")
    _check(intent.volume, "volume_id name")
    if intent.volume.volume_id not in ("", volume_id):
        raise ValueError("Commit intent stable volume differs")
    if not intent.volume.name and not intent.volume.volume_id:
        raise ValueError("Commit intent requires a volume selector")
    if not intent.commit_id:
        raise ValueError("Commit intent requires a caller commit ID")
    if not isinstance(intent.deltas, Oneof) or intent.deltas.field != "path_deltas":
        raise ValueError("Format1 supports inline path deltas only")
    paths = intent.deltas.value
    _check(paths, "upserts removes")
    projection: dict[int, _Value] = {
        1: {1: volume_id},
        2: intent.commit_id,
        3: intent.ref or "main",
        6: [_uploaded(item, volume_id) for item in intent.uploaded_object_references],
        7: intent.author,
        8: {
            1: [_upsert(item, volume_id) for item in paths.upserts],
            2: [_remove(item) for item in paths.removes],
        },
    }
    for tag, field in ((4, "base_version_id"), (5, "base_sequence_number")):
        if intent.has_field(field):
            projection[tag] = _uint(getattr(intent, field))
    return _encode(
        {0: "chalk.volume.commit-request", 1: 1, 2: environment_id, 3: projection}
    )


def commit_request_fingerprint(
    intent: pb.CommitIntent, *, environment_id: str, volume_id: str
) -> bytes:
    return hashlib.sha256(
        canonical_commit_request(
            intent, environment_id=environment_id, volume_id=volume_id
        )
    ).digest()


def _check(message: Any, slots: str) -> None:
    # Generated protobuf-py 0.6 retains unknown wire fields. Refuse both those
    # fields and future generated schema additions until the format covers them.
    if set(type(message).__slots__) != set(slots.split()):
        raise ValueError("Message schema is not fully covered by format1")
    descriptor = type(message)._desc
    signatures = []
    for field in descriptor.fields:
        proto = field.proto
        oneof = "-"
        if proto.has_field("oneof_index"):
            if not 0 <= proto.oneof_index < len(descriptor.proto.oneof_decl):
                raise ValueError("Message descriptor schema is not covered by format1")
            oneof = descriptor.proto.oneof_decl[proto.oneof_index].name
        signatures.append(
            f"{proto.number}:{proto.name}:{int(proto.type)}:{int(proto.label)}:"
            f"{int(field.presence == 1)}:{oneof}:{proto.type_name or '-'}"
        )
    if " ".join(signatures) != _SCHEMAS.get(type(message).__name__):
        raise ValueError("Message descriptor schema is not covered by format1")
    if not hasattr(message, "_unknown_fields") or message._unknown_fields:
        raise ValueError("Unknown protobuf fields are unsupported by format1")


def _uint(value: int, *, maximum: int = 2**64 - 1) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("Integer is outside supported unsigned range")
    return value


def _key(value: str, volume_id: str, keyspace: str) -> str:
    key = value.strip("/")
    if not key or any(part in ("", ".", "..") for part in key.split("/")):
        raise ValueError("Object key contains invalid path segments")
    prefix = f"volumes/{volume_id}/{keyspace}/"
    if key.startswith(prefix):
        return key
    if key.startswith("volumes/"):
        raise ValueError("Object key belongs to a different volume or keyspace")
    return prefix + key


def _uploaded(value: pb.UploadedObjectReference, volume_id: str) -> dict[int, _Value]:
    _check(value, "object_key hash content_size kind")
    kind = int(value.kind)
    if kind not in (1, 2):
        raise ValueError("Format1 supports uploaded CHUNK/PACK references only")
    return {
        1: _key(value.object_key, volume_id, "chunks" if kind == 1 else "packs"),
        2: value.hash,
        3: _uint(value.content_size, maximum=2**63 - 1),
        4: kind,
    }


def _remove(value: pb.PathRemoveDelta) -> dict[int, _Value]:
    _check(value, "path recursive")
    return {1: value.path, 2: value.recursive}


def _upsert(value: pb.PathFileDelta, volume_id: str) -> dict[int, _Value]:
    _check(value, "path node mode")
    if int(value.mode) not in (0, 1):
        raise ValueError("Format1 supports UPSERT write semantics only")
    node = value.node
    if node is None:
        raise ValueError("Upsert requires a file node")
    _check(node, "metadata node")
    if not isinstance(node.node, Oneof) or node.node.field != "file":
        raise ValueError("Format1 supports regular file nodes only")
    file = node.node.value
    _check(file, "content")
    if file.content is None:
        raise ValueError("Regular file requires content")
    encoded: dict[int, _Value] = {2: {1: _content(file.content, volume_id)}}
    if node.metadata is not None:
        metadata = node.metadata
        _check(metadata, "mode updated_at uid gid")
        if any(metadata.has_field(field) for field in ("updated_at", "uid", "gid")):
            raise ValueError("Format1 does not apply timestamp/uid/gid metadata")
        encoded[1] = (
            {1: _uint(metadata.mode, maximum=2**32 - 1)}
            if metadata.has_field("mode")
            else {}
        )
    return {1: value.path, 2: encoded, 3: 1}


def _content(value: pb.ContentRef, volume_id: str) -> dict[int, _Value]:
    _check(value, "content")
    match value.content:
        case Oneof(field="empty", value=empty):
            _check(empty, "")
            return {1: {}}
        case Oneof(field="inline", value=inline):
            _check(inline, "data hash size")
            return {2: {1: inline.data, 2: inline.hash, 3: _uint(inline.size)}}
        case Oneof(field="chunked", value=chunked):
            _check(chunked, "hash size chunks")
            return {
                3: {
                    1: chunked.hash,
                    2: _uint(chunked.size),
                    3: [_chunk(c, volume_id) for c in chunked.chunks],
                }
            }
        case Oneof(field="packed", value=packed):
            _check(packed, "hash size pack")
            if packed.pack is None:
                raise ValueError("Packed content requires a pack reference")
            pack = packed.pack
            _check(pack, "object_key pack_id offset size")
            return {
                4: {
                    1: packed.hash,
                    2: _uint(packed.size),
                    3: {
                        1: _key(pack.object_key, volume_id, "packs"),
                        2: pack.pack_id,
                        3: _uint(pack.offset),
                        4: _uint(pack.size),
                    },
                }
            }
        case _:
            raise ValueError("ContentRef requires a supported explicit content arm")


def _chunk(value: pb.ChunkRef, volume_id: str) -> dict[int, _Value]:
    _check(value, "object_key hash size offset")
    return {
        1: _key(value.object_key, volume_id, "chunks"),
        2: value.hash,
        3: _uint(value.size),
        4: _uint(value.offset),
    }


def _encode(value: _Value) -> bytes:
    output = bytearray()

    def append(data: bytes) -> None:
        if len(output) + len(data) > _MAX_BYTES:
            raise ValueError("Canonical commit request exceeds4MiB bound")
        output.extend(data)

    def head(major: int, size: int) -> None:
        size = _uint(size)
        if size < 24:
            append(bytes([major << 5 | size]))
        else:
            for additional, width in ((24, 1), (25, 2), (26, 4), (27, 8)):
                if size < 1 << (width * 8):
                    append(
                        bytes([major << 5 | additional]) + size.to_bytes(width, "big")
                    )
                    break

    def emit(item: _Value) -> None:
        if isinstance(item, bool):
            append(b"\xf5" if item else b"\xf4")
        elif isinstance(item, int):
            head(0, item)
        elif isinstance(item, bytes):
            head(2, len(item))
            append(item)
        elif isinstance(item, str):
            data = item.encode("utf-8")
            head(3, len(data))
            append(data)
        elif isinstance(item, list):
            head(4, len(item))
            for child in item:
                emit(child)
        elif isinstance(item, dict):
            # All map keys are nonnegative protocol tags, so numeric order
            # equals deterministic encoded-key order for this restricted AST.
            head(5, len(item))
            for key in sorted(item):
                head(0, key)
                emit(item[key])
        else:
            raise ValueError("Unsupported canonical value type")

    emit(value)
    return bytes(output)
