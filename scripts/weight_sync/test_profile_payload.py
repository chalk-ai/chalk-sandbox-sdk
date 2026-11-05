"""Real BF16 publication/reconstruction/reference round trip with in-memory I/O."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from profile_fixture import AUDIT_NAMES, mutate
from profile_cpu_payload import _prepare_fixture


class Volume:
    def __init__(self, name, files=None):
        self.name, self.files = name, files or {}
        self.snapshots = [dict(self.files)]
        self.version = NS(id=0)
        self.version_id = None
        self.ref = "main"

    def reload(self):
        return self

    def at_version(self, version):
        return Volume(self.name, dict(self.snapshots[version]))

    def read_file(self, path):
        from chalkcompute._volume import VolumeFileNotFoundError

        if path not in self.files:
            raise VolumeFileNotFoundError(path)
        return self.files[path]

    def listdir(self, prefix=""):
        return [
            NS(path=p, kind="file")
            for p in self.files
            if not prefix or p.startswith(prefix + "/")
        ]

    def commit(self, files):
        self.files.update(files)
        self.snapshots.append(dict(self.files))
        self.version.id = len(self.snapshots) - 1

    def put_file(self, path, data):
        self.commit({path: data})

    @contextmanager
    def batch_upload(self, **kwargs):
        files = {}
        batch = NS(
            put=lambda p, d: files.update({p: d}),
            put_file=lambda p, f: files.update({p: Path(f).read_bytes()}),
        )
        yield batch
        self.commit(files)


def test_real_small_fixture_reconstructs_every_byte_and_fresh_reference(
    tmp_path, monkeypatch
):
    import torch
    import chalkcompute as cc
    from chalkcompute.weights import WeightStream
    from safetensors.torch import load_file, save_file

    state = {name: torch.arange(1000, dtype=torch.bfloat16) for name in AUDIT_NAMES}
    hf = tmp_path / "hf"
    hf.mkdir()
    save_file(state, str(hf / "model.safetensors"))
    base_volume = Volume("weight-profile-test")
    stream = WeightStream(
        "weight-profile-test", "test/model", volume=base_volume
    ).create()
    stream.publisher(config={"model_type": "qwen3", "hidden_size": 10}).publish(state)
    reference = Volume("weight-profile-test-reference")
    monkeypatch.setattr(
        cc,
        "Volume",
        lambda name: (
            reference if name == reference.name else pytest.fail("unexpected volume")
        ),
    )
    proof = _prepare_fixture(stream, hf, tmp_path / "work", 3, 3000)
    assert proof["whole_model_cpu_bytes_equal"]
    assert proof["actual_changed_elements"] == 30
    assert proof["per_tensor_changed_elements"] == dict.fromkeys(AUDIT_NAMES, 10)
    assert stream.current() == 1 and stream.pinned() == 0
    fresh = WeightStream(reference.name, "test/model", volume=reference).create()
    output = fresh.materialize(0, tmp_path / "fresh")
    actual = load_file(str(output / "model.safetensors"))
    assert set(actual) == set(state)
    for name in state:
        assert torch.equal(
            actual[name].view(torch.int16), mutate(state[name], 10).view(torch.int16)
        )
    assert (
        proof["base_three_parameter_hashes"] != proof["target_three_parameter_hashes"]
    )
