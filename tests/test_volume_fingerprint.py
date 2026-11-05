from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from protobuf import Oneof

from chalkcompute._gen.chalk.volume.v2 import volume_pb as pb
from chalkcompute._volume_fingerprint import (
    canonical_commit_request,
    commit_request_fingerprint,
)


def _intent(content: pb.ContentRef | None = None) -> pb.CommitIntent:
    return pb.CommitIntent(
        volume=pb.VolumeRef(name="models"),
        commit_id="attempt-1",
        base_sequence_number=0,
        author="trainer",
        deltas=Oneof(
            "path_deltas",
            pb.PathDeltaList(
                upserts=[
                    pb.PathFileDelta(
                        path="CURRENT",
                        node=pb.FileNode(
                            node=Oneof(
                                "file",
                                pb.RegularFileNode(
                                    content=content
                                    or pb.ContentRef(
                                        content=Oneof("empty", pb.EmptyFileContent())
                                    )
                                ),
                            )
                        ),
                    )
                ],
                removes=[pb.PathRemoveDelta(path="obsolete", recursive=True)],
            ),
        ),
    )


def _bytes(intent: pb.CommitIntent, **kwargs: str) -> bytes:
    return canonical_commit_request(
        intent,
        environment_id=kwargs.get("environment_id", "env-1"),
        volume_id=kwargs.get("volume_id", "vol-1"),
    )


def _upsert(intent: pb.CommitIntent) -> Any:
    assert intent.deltas is not None
    return intent.deltas.value.upserts[0]


_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "volume_commit_format1.json").read_text()
)


@pytest.mark.parametrize("vector", _VECTORS, ids=[item["name"] for item in _VECTORS])
def test_frozen_cross_language_wire_vector(vector: dict[str, Any]) -> None:
    intent = pb.CommitIntent.from_binary(bytes.fromhex(vector["wire_hex"]))
    identity = {
        "environment_id": vector["environment_id"],
        "volume_id": vector["volume_id"],
    }
    if vector["reject"]:
        with pytest.raises(ValueError):
            canonical_commit_request(intent, **identity)
    else:
        canonical = canonical_commit_request(intent, **identity)
        assert canonical.hex() == vector["canonical_hex"]
        assert hashlib.sha256(canonical).hexdigest() == vector["sha256"]


def test_future_schema_field_cannot_be_silently_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent()
    monkeypatch.setattr(
        pb.PathDeltaList, "__slots__", (*pb.PathDeltaList.__slots__, "future_field")
    )
    with pytest.raises(ValueError, match="schema is not fully covered"):
        _bytes(intent)


def test_identity_aliases_are_canonical_and_inputs_unchanged() -> None:
    original = _intent()
    before = original.to_binary()
    alias = pb.CommitIntent.from_binary(before)
    alias.volume = pb.VolumeRef(volume_id="vol-1")
    alias.ref = "main"
    _upsert(alias).mode = pb.PathWriteMode.UPSERT
    assert _bytes(original) == _bytes(alias)
    assert original.to_binary() == before
    assert (
        commit_request_fingerprint(original, environment_id="env-1", volume_id="vol-1")
        == hashlib.sha256(_bytes(original)).digest()
    )


@pytest.mark.parametrize("field", ["base_sequence_number", "base_version_id"])
def test_base_presence_zero_and_max_uint64_are_distinct(field: str) -> None:
    absent = _intent()
    absent.clear_field("base_sequence_number")
    zero = pb.CommitIntent.from_binary(absent.to_binary())
    setattr(zero, field, 0)
    maximum = pb.CommitIntent.from_binary(absent.to_binary())
    setattr(maximum, field, 2**64 - 1)
    assert len({_bytes(absent), _bytes(zero), _bytes(maximum)}) == 3


def test_environment_volume_author_and_order_bind_identity() -> None:
    first = _intent()
    assert _bytes(first) != _bytes(first, environment_id="env-2")
    assert _bytes(first) != _bytes(first, volume_id="vol-2")
    other = pb.CommitIntent.from_binary(first.to_binary())
    other.author = "another trainer"
    assert _bytes(first) != _bytes(other)
    assert other.deltas is not None
    other.deltas.value.removes.append(pb.PathRemoveDelta(path="second"))
    ordered = _bytes(other)
    other.deltas.value.removes.reverse()
    assert _bytes(other) != ordered


def test_inline_binary_empty_and_absent_content_are_distinct() -> None:
    explicit_empty = _intent()
    inline_empty = _intent(
        pb.ContentRef(content=Oneof("inline", pb.InlineFileContent()))
    )
    inline_bytes = _intent(
        pb.ContentRef(
            content=Oneof(
                "inline",
                pb.InlineFileContent(data=b"\x00\xff", size=2, hash="declared-hash"),
            )
        )
    )
    assert (
        len({_bytes(explicit_empty), _bytes(inline_empty), _bytes(inline_bytes)}) == 3
    )
    _upsert(explicit_empty).node.node.value.content = pb.ContentRef()
    with pytest.raises(ValueError, match="explicit content arm"):
        _bytes(explicit_empty)


def test_metadata_absent_empty_and_zero_presence_are_distinct() -> None:
    intent = _intent()
    absent = _bytes(intent)
    _upsert(intent).node.metadata = pb.FileMetadata()
    empty = _bytes(intent)
    _upsert(intent).node.metadata.mode = 0
    zero = _bytes(intent)
    assert len({absent, empty, zero}) == 3


@pytest.mark.parametrize("field", ["uid", "gid"])
def test_ignored_metadata_is_rejected_even_at_zero(field: str) -> None:
    intent = _intent()
    _upsert(intent).node.metadata = pb.FileMetadata(**{field: 0})
    with pytest.raises(ValueError, match="metadata"):
        _bytes(intent)


@pytest.mark.parametrize(
    "mode", [pb.PathWriteMode.CREATE, pb.PathWriteMode.REPLACE, 99]
)
def test_unsupported_write_modes_rejected(mode: Any) -> None:
    intent = _intent()
    _upsert(intent).mode = mode
    with pytest.raises(ValueError, match="UPSERT"):
        _bytes(intent)


def test_unknown_wire_fields_preserved_and_rejected_recursively() -> None:
    unknown = bytes.fromhex("a00601")
    intent = pb.CommitIntent.from_binary(_intent().to_binary() + unknown)
    assert unknown in intent.to_binary()
    with pytest.raises(ValueError, match="Unknown protobuf"):
        _bytes(intent)
    nested = _intent()
    metadata = pb.FileMetadata.from_binary(unknown)
    assert metadata.to_binary() == unknown
    _upsert(nested).node.metadata = metadata
    with pytest.raises(ValueError, match="Unknown protobuf"):
        _bytes(nested)


def test_chunk_keys_normalize_and_all_ranges_bind_identity() -> None:
    intent = _intent(
        pb.ContentRef(
            content=Oneof(
                "chunked",
                pb.ChunkedContentRef(
                    hash="file-hash",
                    size=2,
                    chunks=[
                        pb.ChunkRef(
                            object_key="/object/", hash="chunk-hash", size=2, offset=3
                        )
                    ],
                ),
            )
        )
    )
    intent.uploaded_object_references.append(
        pb.UploadedObjectReference(
            object_key="object",
            hash="object-hash",
            content_size=5,
            kind=pb.UploadedObjectKind.CHUNK,
        )
    )
    canonical = _bytes(intent)
    chunk = _upsert(intent).node.node.value.content.content.value.chunks[0]
    chunk.object_key = "volumes/vol-1/chunks/object"
    intent.uploaded_object_references[0].object_key = chunk.object_key
    assert _bytes(intent) == canonical
    chunk.offset = 4
    assert _bytes(intent) != canonical
    chunk.object_key = "volumes/other/chunks/object"
    with pytest.raises(ValueError, match="different volume"):
        _bytes(intent)


def test_packed_ranges_and_pack_identity_bind_request() -> None:
    intent = _intent(
        pb.ContentRef(
            content=Oneof(
                "packed",
                pb.PackedContentRef(
                    hash="file-hash",
                    size=2,
                    pack=pb.PackEntryRef(
                        object_key="pack-object", pack_id="pack-1", offset=3, size=2
                    ),
                ),
            )
        )
    )
    canonical = _bytes(intent)
    pack = _upsert(intent).node.node.value.content.content.value.pack
    pack.object_key = "volumes/vol-1/packs/pack-object"
    assert _bytes(intent) == canonical
    pack.pack_id = "pack-2"
    assert _bytes(intent) != canonical


def test_wire_and_canonical_size_bounds() -> None:
    intent = _intent(
        pb.ContentRef(
            content=Oneof("inline", pb.InlineFileContent(data=b"x" * (4 * 1024 * 1024)))
        )
    )
    with pytest.raises(ValueError, match="wire bound"):
        _bytes(intent)
    from chalkcompute._volume_fingerprint import _encode

    with pytest.raises(ValueError, match="Canonical.*bound"):
        _encode({0: b"x" * (4 * 1024 * 1024)})


@pytest.mark.parametrize(
    ("attribute", "value"),
    [("type", 12), ("label", 3), ("oneof_index", 0)],
)
def test_same_name_descriptor_change_requires_review(
    monkeypatch: pytest.MonkeyPatch, attribute: str, value: int
) -> None:
    # Tags and slots alone do not cover a same-name kind/cardinality/oneof edit.
    proto = (
        pb.CommitIntent._desc.fields[2].proto
        if attribute == "oneof_index"
        else pb.PathRemoveDelta._desc.fields[0].proto
    )
    monkeypatch.setattr(proto, attribute, value)
    with pytest.raises(ValueError, match="descriptor schema"):
        _bytes(_intent())
