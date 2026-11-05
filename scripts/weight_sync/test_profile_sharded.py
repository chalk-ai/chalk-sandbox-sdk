"""Actual SDK codec/manifest/batch boundaries with isolated fake remote I/O."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, cast

import pytest

from profile_sharded import prepare
from profile_streaming import generate
from test_profile_payload import Volume as MemoryVolume


class Volume(MemoryVolume):
    def __init__(self, name):
        super().__init__(name)
        self.uploads = []
        self.fail_before = self.fail_after = None
        self.corrupt_download = False
        self.corrupt_patch = False
        self._client = NS(_upload=self.upload)

    def upload(self, name, files, **kwargs):
        assert name == self.name
        self.uploads.append([name for name, _ in files])
        if self.fail_before == len(self.uploads):
            raise RuntimeError("staged upload failed")
        self.commit({p: f.read_bytes() if isinstance(f, Path) else f for p, f in files})
        if self.fail_after == len(self.uploads):
            raise RuntimeError("commit response lost")
        return []

    def _selector_ref(self):
        return None

    def _update_version_from_statuses(self, statuses):
        return self.version

    @contextmanager
    def batch_upload(self, **kwargs):
        from chalkcompute._volume import _BatchUpload

        batch = _BatchUpload(cast(Any, self), **kwargs)
        yield batch
        batch._flush()

    def at_version(self, version):
        snapshot = MemoryVolume(self.name, dict(self.snapshots[version]))
        snapshot.version_id = version
        if self.corrupt_patch and "v000001/patches.bin" in snapshot.files:
            blob = snapshot.files["v000001/patches.bin"]
            snapshot.files["v000001/patches.bin"] = blob[:-1] + bytes([blob[-1] ^ 1])

        def download(path, target):
            data = snapshot.read_file(path)
            if self.corrupt_download:
                data = data[:-1] + bytes([data[-1] ^ 1])
            Path(target).write_bytes(data)

        setattr(snapshot, "download_to_file", download)
        return snapshot


def fixture(tmp_path, tensors=7, elements=2048):
    import importlib

    WeightStream = cast(
        Any, importlib.import_module("chalkcompute.weights")
    ).WeightStream

    source = tmp_path / "source"
    generate(source, tensors=tensors, elements=elements, shard_tensors=2)
    stream_volume, ref_volume = Volume("stream"), Volume("reference")
    stream = WeightStream("stream", "synthetic/fixture", volume=stream_volume)
    reference = WeightStream("reference", "synthetic/fixture", volume=ref_volume)
    return (
        source,
        stream,
        reference,
        dict(expected_tensors=tensors, expected_elements=tensors * elements),
    )


def test_real_grouped_uploads_single_atomic_patch_and_independent_sharded_reference(
    tmp_path,
):
    import json

    from chalkcompute._weights_format import read_pointer

    source, stream, reference, options = fixture(tmp_path)
    proof = prepare(stream, reference, source, tmp_path / "work", **options)
    assert (
        proof["whole_model_cpu_bytes_equal"]
        and proof["independent_literal_oracle_bytes_equal"]
    )
    assert proof["actual_changed_elements"] == 7 * 2048 // 100
    assert proof["reference_shards"] == 4
    assert len(stream.volume.uploads) == 2 and len(reference.volume.uploads) == 1
    assert set(stream.volume.uploads[1]) == {
        "v000001/patches.bin",
        "v000001/manifest.json",
        "CURRENT",
    }
    assert read_pointer(stream.volume.snapshots[1]["CURRENT"]) == 0
    assert read_pointer(stream.volume.snapshots[2]["CURRENT"]) == 1
    snapshot = reference.volume.at_version(proof["reference_snapshot_id"])
    index = json.loads(snapshot.read_file("v000000/model.safetensors.index.json"))
    assert len(index["weight_map"]) == 7
    # The actual SDK consumer also accepts both sharded streams.
    live = stream.materialize(1, tmp_path / "materialized-live")
    fresh = reference.materialize(0, tmp_path / "materialized-reference")
    from safetensors.torch import load_file
    from profile_fixture import digest

    live_state = load_file(str(live / "model.safetensors"))
    fresh_state = load_file(str(fresh / "model.safetensors"))
    assert {n: digest(t) for n, t in live_state.items()} == {
        n: digest(t) for n, t in fresh_state.items()
    }


@pytest.mark.parametrize("when", ["fail_before", "fail_after"])
def test_patch_crash_preserves_atomic_pointer_and_refuses_replay(tmp_path, when):
    source, stream, reference, options = fixture(tmp_path)
    setattr(stream.volume, when, 2)
    with pytest.raises(RuntimeError, match="failed|lost"):
        prepare(stream, reference, source, tmp_path / "work", **options)
    assert stream.current() == (0 if when == "fail_before" else 1)
    assert (tmp_path / "work" / "patch-attempt.json").is_file()
    assert reference.current() is None
    with pytest.raises(FileExistsError):
        prepare(stream, reference, source, tmp_path / "work", **options)
    assert len(stream.volume.uploads) == 2
    assert ("v000001/manifest.json" in stream.volume.files) == (stream.current() == 1)
    assert ("v000001/patches.bin" in stream.volume.files) == (stream.current() == 1)


def test_corrupt_remote_baseline_fails_before_patch_commit(tmp_path):
    source, stream, reference, options = fixture(tmp_path)
    stream.volume.corrupt_download = True
    with pytest.raises(RuntimeError, match="checksum"):
        prepare(stream, reference, source, tmp_path / "work", **options)
    assert stream.current() == 0 and len(stream.volume.uploads) == 1
    assert reference.current() is None


def test_corrupt_persisted_remote_patch_fails_before_reference_commit(tmp_path):
    from chalkcompute._weights_format import WeightStreamError

    source, stream, reference, options = fixture(tmp_path)
    stream.volume.corrupt_patch = True
    with pytest.raises(WeightStreamError, match="checksum|Checksum"):
        prepare(stream, reference, source, tmp_path / "work", **options)
    assert stream.current() == 1 and reference.current() is None


@pytest.mark.parametrize("when", ["fail_before", "fail_after"])
def test_reference_crash_never_changes_proved_stream_snapshot(tmp_path, when):
    source, stream, reference, options = fixture(tmp_path)
    setattr(reference.volume, when, 1)
    with pytest.raises(RuntimeError, match="failed|lost"):
        prepare(stream, reference, source, tmp_path / "work", **options)
    assert stream.current() == 1
    assert reference.current() == (None if when == "fail_before" else 0)
    assert (tmp_path / "work" / "reference-attempt.json").is_file()
    with pytest.raises(RuntimeError, match="empty volumes"):
        prepare(stream, reference, source, tmp_path / "new-work", **options)
    assert len(reference.volume.uploads) == 1


def test_wrong_hf_index_rejected_before_remote_commit(tmp_path):
    source, stream, reference, options = fixture(tmp_path)
    (source / "model.safetensors.index.json").write_text('{"weight_map":{}}')
    with pytest.raises(RuntimeError, match="index canonical file mapping"):
        prepare(stream, reference, source, tmp_path / "work", **options)
    assert not stream.volume.uploads


def test_nonempty_volume_rejected_without_current_pointer(tmp_path):
    source, stream, reference, options = fixture(tmp_path)
    reference.volume.files["unowned-data"] = b"keep"
    with pytest.raises(RuntimeError, match="empty volumes"):
        prepare(stream, reference, source, tmp_path / "work", **options)
    assert (
        not stream.volume.uploads and reference.volume.files["unowned-data"] == b"keep"
    )


def test_source_mutation_after_baseline_is_detected(tmp_path):
    import torch
    from safetensors.torch import load_file, save_file

    source, stream, reference, options = fixture(tmp_path)

    def change_source(stage):
        if stage == "baseline_verified":
            shard = next(source.glob("*.safetensors"))
            state = {n: t.clone() for n, t in load_file(str(shard)).items()}
            for tensor in state.values():
                tensor.view(torch.int16)[0] ^= 2
            save_file(state, str(shard))

    with pytest.raises(RuntimeError, match="Immutable source checkpoint"):
        prepare(
            stream,
            reference,
            source,
            tmp_path / "work",
            sample=change_source,
            **options,
        )
    assert len(stream.volume.uploads) == 1 and reference.current() is None
