from dataclasses import replace

import pytest


from chalkcompute._weights_codec import (
    apply_patch,
    checksum,
    decode_patch,
    diff,
    encode_patch,
    verify_payloads,
)
from chalkcompute._weights_format import (
    TensorEntry,
    WeightManifest,
    WeightStreamError,
    pointer_json,
    read_pointer,
    version_dir,
)


torch = pytest.importorskip("torch")
pytest.importorskip("zstandard")
pytest.importorskip("xxhash")


def entry_for(patch, payload):
    return TensorEntry(
        patch.name,
        patch.dtype,
        patch.shape,
        patch.encoding,
        "patches.bin",
        0,
        len(payload),
        patch.nnz,
        checksum(payload),
    )


@pytest.mark.parametrize(
    "dtype",
    [
        torch.bfloat16,
        torch.float16,
        torch.float32,
        torch.float64,
        torch.int64,
        torch.uint8,
        torch.bool,
    ],
)
@pytest.mark.parametrize("density", [0.05, 1.0])
def test_roundtrip_and_idempotent_absolute_apply(dtype, density):
    old = torch.zeros(100, dtype=dtype)
    new = old.clone()
    new[: int(100 * density)] = 1
    patch = diff(old, new, name="model.weight")
    assert patch.encoding == ("sparse" if density <= 0.3 else "dense")
    payload = encode_patch(patch)
    decoded = decode_patch(entry_for(patch, payload), payload)
    apply_patch(old, decoded)
    apply_patch(old, decoded)
    assert torch.equal(old, new)


def test_signed_zero_and_nan_storage_bits_are_preserved():
    old = torch.tensor([0.0, float("nan"), 1.0])
    new = old.clone()
    new.view(torch.int32)[0] = -(2**31)
    new.view(torch.int32)[1] = 0x7FC00001
    patch = diff(old, new, name="w")
    assert patch is not None
    payload = encode_patch(patch)
    apply_patch(old, decode_patch(entry_for(patch, payload), payload))
    assert torch.equal(old.view(torch.int32), new.view(torch.int32))


def test_no_change_does_not_create_patch():
    x = torch.arange(20.0)
    assert diff(x, x.clone()) is None


def test_shape_or_dtype_change_refused():
    with pytest.raises(WeightStreamError):
        diff(torch.zeros(2), torch.zeros(3))
    with pytest.raises(WeightStreamError):
        diff(torch.zeros(2), torch.zeros(2, dtype=torch.bfloat16))


def test_corrupt_payload_refused_before_apply():
    patch = diff(torch.zeros(10), torch.ones(10), name="w")
    payload = encode_patch(patch)
    with pytest.raises(WeightStreamError, match="Checksum"):
        decode_patch(entry_for(patch, payload), bytes([payload[0] ^ 1]) + payload[1:])


def test_shard_offsets_verified_and_truncation_refused(tmp_path):
    patch = diff(torch.zeros(10), torch.ones(10), name="w")
    payload = encode_patch(patch)
    entry = replace(entry_for(patch, payload), offset=3)
    manifest = WeightManifest(1, 0, "patch", "model", "a" * 64, (entry,))
    (tmp_path / "patches.bin").write_bytes(b"pad" + payload)
    verify_payloads(tmp_path, manifest)
    (tmp_path / "patches.bin").write_bytes(b"pad" + payload[:-1])
    with pytest.raises(WeightStreamError, match="Truncated"):
        verify_payloads(tmp_path, manifest)


def test_symlink_cannot_escape_version_dir(tmp_path):
    outside = tmp_path.parent / "outside"
    outside.write_bytes(b"x")
    (tmp_path / "patches.bin").symlink_to(outside)
    entry = TensorEntry(
        "w", "uint8", (1,), "dense", "patches.bin", 0, 1, 1, checksum(b"x")
    )
    manifest = WeightManifest(0, None, "keyframe", "model", "a" * 64, (entry,))
    with pytest.raises(WeightStreamError, match="escapes"):
        verify_payloads(tmp_path, manifest)


@pytest.mark.parametrize("path", ["../x", "/absolute", "sub/../../x", "windows\\x"])
def test_manifest_rejects_unsafe_paths(path):
    with pytest.raises(WeightStreamError):
        TensorEntry("w", "float32", (1,), "dense", path, 0, 1, 1, "0" * 32)


def test_manifest_and_pointers_roundtrip():
    entry = TensorEntry("w", "float32", (1,), "dense", "weights.bin", 0, 4, 1, "0" * 32)
    manifest = WeightManifest(0, None, "keyframe", "model", "a" * 64, (entry,))
    assert WeightManifest.from_json(manifest.to_json()) == manifest
    assert read_pointer(pointer_json(23)) == 23
    assert read_pointer(pointer_json(None)) is None
    assert version_dir(23) == "v000023"
    for invalid in [
        b"{",
        b'{"schema_version":2,"version":1}',
        b'{"schema_version":1,"version":true}',
    ]:
        with pytest.raises(WeightStreamError):
            read_pointer(invalid)


@pytest.mark.parametrize("changed", [2, 40])
def test_diff_counts_actual_elements_before_dense_fallback(changed):
    old = torch.zeros(100)
    new = old.clone()
    new[:changed] = 1
    patch = diff(old, new, name="w")
    assert patch.changed_elements == changed
    decoded = decode_patch(entry_for(patch, encode_patch(patch)), encode_patch(patch))
    # The patch payload format does not store the old state or actual density.
    assert decoded.changed_elements is None
