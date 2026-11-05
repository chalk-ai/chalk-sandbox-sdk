import json

import pytest


from chalkcompute.weights import WeightStream, WeightStreamError
from tests._fakes import MemoryWeightVolume


torch = pytest.importorskip("torch")
pytest.importorskip("zstandard")
pytest.importorskip("xxhash")
load_file = pytest.importorskip("safetensors.torch").load_file


@pytest.fixture
def stream():
    return WeightStream("test", "test/model", volume=MemoryWeightVolume()).create()


def publisher(stream, **kwargs):
    return stream.publisher(config={"model_type": "qwen3", "hidden_size": 10}, **kwargs)


def test_publish_atomic_keyframe_sparse_patch_restore_and_materialize(stream, tmp_path):
    pub = publisher(stream)
    state = {"model.weight": torch.zeros(100, dtype=torch.bfloat16)}
    assert pub.publish(state, step=0) == 0
    assert stream.current() == 0
    first = stream._volume.commits[-1]
    assert set(first) == {
        "v000000/model.safetensors",
        "v000000/config.json",
        "v000000/manifest.json",
        "CURRENT",
    }
    state["model.weight"][3] = 1
    assert pub.publish(state, step=1) == 1
    assert stream.versions()[-1].tensors[0].encoding == "sparse"
    state["model.weight"][3] = 0
    assert pub.publish(state, step=2) == 2
    path = stream.materialize(2, tmp_path / "restored")
    assert torch.equal(
        load_file(str(path / "model.safetensors"))["model.weight"],
        torch.zeros(100, dtype=torch.bfloat16),
    )
    assert json.loads((path / "config.json").read_text())["model_type"] == "qwen3"
    assert stream.versions()[-1].trainer_step == 2


def test_snapshot_does_not_alias_training_state(stream, tmp_path):
    pub = publisher(stream)
    weights = torch.zeros(10)
    pub.publish({"w": weights})
    weights[0] = 5
    pub.publish({"w": weights})
    checkpoint = stream.materialize(1, tmp_path / "v1")
    assert load_file(str(checkpoint / "model.safetensors"))["w"][0] == 5


def test_pin_rollback_replays_from_keyframe_before_target(stream, tmp_path):
    pub = publisher(stream, keyframe_every=2)
    for step in range(5):
        pub.publish({"w": torch.full((10,), float(step))})
    stream.pin(1)
    assert stream.pinned() == 1
    checkpoint = stream.materialize(1, tmp_path / "pinned")
    assert torch.equal(
        load_file(str(checkpoint / "model.safetensors"))["w"], torch.ones(10)
    )
    assert stream.current() == 4
    stream.unpin()
    assert stream.pinned() is None


def test_publishing_preserves_pin_disjoint_commits(stream):
    pub = publisher(stream)
    pub.publish({"w": torch.zeros(10)})
    stream.pin(0)
    pub.publish({"w": torch.ones(10)})
    assert stream.pinned() == 0
    assert stream.current() == 1
    assert "PIN" not in stream._volume.commits[-1]


def test_failure_keeps_publisher_and_current_at_previous_version(stream):
    pub = publisher(stream)
    pub.publish({"w": torch.zeros(10)})
    stream._volume.fail_commit = True
    with pytest.raises(RuntimeError, match="injected"):
        pub.publish({"w": torch.ones(10)})
    stream._volume.fail_commit = False
    assert stream.current() == 0
    assert pub.publish({"w": torch.ones(10)}) == 1


def test_stale_publisher_refused(stream):
    first, second = publisher(stream), publisher(stream)
    first.publish({"w": torch.zeros(10)})
    with pytest.raises(WeightStreamError, match="stale"):
        second.publish({"w": torch.ones(10)})


def test_architecture_change_refused(stream):
    pub = publisher(stream)
    pub.publish({"w": torch.zeros(10)})
    with pytest.raises(WeightStreamError, match="architecture"):
        pub.publish({"w": torch.zeros(11)})


def test_prune_preserves_pin_replay_chain(stream, tmp_path):
    pub = publisher(stream, keyframe_every=2)
    for step in range(7):
        pub.publish({"w": torch.full((10,), float(step))})
    stream.pin(1)
    assert stream.prune(keep_keyframes=1) == []
    stream.unpin()
    assert stream.prune(keep_keyframes=2) == [0, 1, 2, 3]
    assert [m.version for m in stream.versions()] == [4, 5, 6]
    path = stream.materialize(5, tmp_path / "v5")
    assert torch.equal(
        load_file(str(path / "model.safetensors"))["w"], torch.full((10,), 5.0)
    )


def test_corruption_refused_without_partial_materialized_output(stream, tmp_path):
    pub = publisher(stream)
    pub.publish({"w": torch.zeros(10)})
    pub.publish({"w": torch.ones(10)})
    stream._volume.put_file("v000001/patches.bin", b"broken")
    with pytest.raises(WeightStreamError, match="Checksum"):
        stream.materialize(1, tmp_path / "broken")
    assert not (tmp_path / "broken").exists()


def test_resume_requires_exact_weights(stream):
    pub = publisher(stream)
    pub.publish({"w": torch.zeros(10)})
    resumed = publisher(stream)
    with pytest.raises(WeightStreamError, match="does not match"):
        resumed.resume({"w": torch.ones(10)})
    resumed.resume({"w": torch.zeros(10)})
    assert resumed.publish({"w": torch.ones(10)}) == 1


def test_too_many_keyframe_assets_refused_before_any_commit(stream, tmp_path):
    from chalkcompute._weights_format import WeightManifest

    files = []
    for number in range(199):
        path = tmp_path / str(number)
        path.write_bytes(b"x")
        files.append((path.name, path))
    manifest = WeightManifest(0, None, "keyframe", "test", "a" * 64, ())
    with pytest.raises(WeightStreamError, match="200-file"):
        stream._commit(manifest, files)
    assert stream._volume.commits == []


def test_existing_materialization_destination_refused(stream, tmp_path):
    pub = publisher(stream)
    pub.publish({"w": torch.zeros(10)})
    with pytest.raises(FileExistsError):
        stream.materialize(0, tmp_path)


@pytest.mark.parametrize("dtype", [torch.float32, torch.bfloat16])
def test_scalar_state_publication_and_resume(stream, tmp_path, dtype):
    pub = publisher(stream)
    pub.publish({"scalar": torch.tensor(1.0, dtype=dtype)})
    resumed = publisher(stream)
    resumed.resume({"scalar": torch.tensor(1.0, dtype=dtype)})
    resumed.publish({"scalar": torch.tensor(2.0, dtype=dtype)})
    path = stream.materialize(1, tmp_path / "scalar")
    assert load_file(str(path / "model.safetensors"))["scalar"].item() == 2


def test_seed_and_periodic_keyframe_preserve_tokenizer_assets(
    stream, tmp_path, monkeypatch
):
    from safetensors.torch import save_file
    import huggingface_hub

    source = (
        tmp_path
        / ".cache"
        / "huggingface"
        / "hub"
        / "models--test--model"
        / "snapshots"
        / "pinned"
    )
    source.mkdir(parents=True)
    (source / "config.json").write_text('{"model_type":"qwen3","hidden_size":10}')
    (source / "tokenizer.json").write_text('{"tokenizer":"test"}')
    (source / "generation_config.json").write_text('{"eos_token_id":1}')
    save_file({"w": torch.zeros(10)}, str(source / "model.safetensors"))
    monkeypatch.setattr(
        huggingface_hub, "snapshot_download", lambda **kwargs: str(source)
    )
    assert stream.seed_from_hf("test/model", revision="pinned-test-revision") == 0
    pub = stream.publisher(keyframe_every=2)
    pub.resume({"w": torch.zeros(10)})
    pub.publish({"w": torch.ones(10)})
    pub.publish({"w": torch.full((10,), 2.0)})
    assert stream.versions()[-1].kind == "keyframe"
    checkpoint = stream.materialize(2, tmp_path / "checkpoint")
    assert (checkpoint / "tokenizer.json").read_bytes() == (
        source / "tokenizer.json"
    ).read_bytes()
    assert (checkpoint / "generation_config.json").is_file()


def test_seed_refuses_oversized_snapshot_without_partial_publish(
    stream, tmp_path, monkeypatch
):
    import huggingface_hub

    (tmp_path / "config.json").write_text("{}")
    for number in range(199):
        (tmp_path / f"asset-{number}.json").write_text("{}")
    monkeypatch.setattr(
        huggingface_hub, "snapshot_download", lambda **kwargs: str(tmp_path)
    )
    with pytest.raises(WeightStreamError, match="200-file"):
        stream.seed_from_hf("test/model", revision="pinned-test-revision")
    assert stream._volume.commits == []


def test_reopened_stream_resume_infers_base_model(stream):
    publisher(stream).publish({"w": torch.zeros(10)})
    reopened = WeightStream("test", volume=stream.volume)
    pub = reopened.publisher()
    pub.resume({"w": torch.zeros(10)})
    assert reopened.base_model == "test/model"
    assert pub.publish({"w": torch.ones(10)}) == 1


def test_reopened_stream_rejects_explicit_wrong_base_model(stream):
    publisher(stream).publish({"w": torch.zeros(10)})
    reopened = WeightStream("test", "wrong/model", volume=stream.volume)
    with pytest.raises(WeightStreamError, match="base_model"):
        reopened.publisher().resume({"w": torch.zeros(10)})


@pytest.mark.parametrize(
    "changed", [lambda t: t.reshape(2, 5), lambda t: t.view(torch.int32)]
)
def test_resume_rejects_shape_or_dtype_even_when_storage_bytes_match(stream, changed):
    publisher(stream).publish({"w": torch.zeros(10)})
    with pytest.raises(WeightStreamError, match="does not match"):
        publisher(stream).resume({"w": changed(torch.zeros(10))})
