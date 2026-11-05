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


def test_publication_profile_reports_actual_changes_and_dense_transmission(stream):
    pub = publisher(stream)
    assert pub.last_publish_stats is None
    state = {"w": torch.zeros(100), "unchanged": torch.ones(50)}
    pub.publish(state)
    seed = pub.last_publish_stats
    assert seed["status"] == "published"
    assert seed["changed_elements"] == seed["transmitted_elements"] == 150
    assert seed["volume_version_id"] == 1
    state["w"][:40] = 1
    pub.publish(state)
    stats = pub.last_publish_stats
    assert stats["version"] == 1
    assert stats["kind"] == "patch"
    assert stats["total_elements"] == 150
    assert stats["changed_elements"] == 40
    assert stats["transmitted_elements"] == 100
    assert stats["payload_bytes"] > 0
    assert stats["input_devices"] == ["cpu"]
    assert stats["torch_num_threads"] > 0
    assert stats["torch_num_interop_threads"] > 0
    assert stats["total_seconds"] >= sum(stats["phases_seconds"].values())
    assert all(v >= 0 for v in stats["phases_seconds"].values())
    json.dumps(stats)
    stats["phases_seconds"]["diff"] = -1
    assert pub.last_publish_stats["phases_seconds"]["diff"] >= 0
    pub.publish(state)
    assert pub.last_publish_stats["changed_elements"] == 0
    assert pub.last_publish_stats["transmitted_elements"] == 0
    pub.publish_keyframe(state)
    assert pub.last_publish_stats["changed_elements"] is None
    assert pub.last_publish_stats["transmitted_elements"] == 150


def test_failed_commit_profile_is_not_reported_published(stream):
    pub = publisher(stream)
    stream._volume.fail_commit = True
    with pytest.raises(RuntimeError, match="injected"):
        pub.publish({"w": torch.zeros(10)})
    assert pub.last_publish_stats["status"] == "failed"
    assert pub.last_publish_stats["volume_version_id"] is None
    assert pub.last_publish_stats["total_seconds"] >= 0


def test_profile_captures_committed_snapshot_when_refresh_fails(stream, monkeypatch):
    pub = publisher(stream)
    current = stream.current

    def fail_after_commit():
        if stream._volume.version.id:
            raise RuntimeError("postcommit refresh unavailable")
        return current()

    monkeypatch.setattr(stream, "current", fail_after_commit)
    with pytest.raises(RuntimeError, match="postcommit refresh"):
        pub.publish({"w": torch.zeros(10)})
    stats = pub.last_publish_stats
    assert stats["status"] == "refresh_failed"
    assert stats["volume_version_id"] == 1
    assert stats["version"] == 0
    assert stats["phases_seconds"]["post_commit_refresh"] >= 0
    monkeypatch.setattr(stream, "current", current)
    assert stream.current() == 0
    # Publication did commit; a caller must resume/reconcile rather than reuse 0.
    with pytest.raises(WeightStreamError, match="stale"):
        pub.publish({"w": torch.zeros(10)})
    pub.resume({"w": torch.zeros(10)})
    assert pub.publish({"w": torch.ones(10)}) == 1


def output_volume():
    output = MemoryWeightVolume()
    output.name = "checkpoint-output"
    return output


def seeded_with_tokenizer(stream, monkeypatch):
    # Match seed_from_hf assets while keeping this test independent of the Hub.
    original_commit = stream._commit

    def commit_with_tokenizer(manifest, files, **kwargs):
        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as directory:
            tokenizer = Path(directory) / "tokenizer.json"
            tokenizer.write_text('{"version":"1.0"}')
            tokenizer_config = Path(directory) / "tokenizer_config.json"
            tokenizer_config.write_text('{"tokenizer_class":"PreTrainedTokenizerFast"}')
            return original_commit(
                manifest,
                [
                    *files,
                    (tokenizer.name, tokenizer),
                    (tokenizer_config.name, tokenizer_config),
                ],
                **kwargs,
            )

    monkeypatch.setattr(stream, "_commit", commit_with_tokenizer)
    pub = publisher(stream, keyframe_every=2)
    pub.publish({"w": torch.zeros(10)})
    pub.publish({"w": torch.ones(10)})
    source = stream.volume.version.id
    monkeypatch.setattr(stream, "_commit", original_commit)
    return pub, source


def test_immutable_materialize_ignores_live_current_pin_and_prune(
    stream, tmp_path, monkeypatch
):
    pub, source = seeded_with_tokenizer(stream, monkeypatch)
    pub.publish({"w": torch.full((10,), 2.0)})
    pub.publish({"w": torch.full((10,), 3.0)})
    stream.pin(3)
    stream.prune(keep_keyframes=1)
    assert [m.version for m in stream.versions()] == [2, 3]
    from chalkcompute._volume import VolumeFileNotFoundError

    with pytest.raises(VolumeFileNotFoundError):
        stream.materialize(1, tmp_path / "pruned")

    def no_mutable_snapshot():
        raise AssertionError("immutable replay must never read main")

    monkeypatch.setattr(stream, "_snapshot", no_mutable_snapshot)
    output = output_volume()
    result = stream.materialize_to_volume(
        1, source_volume_version_id=source, destination=output
    )
    assert result.volume_name == output.name
    assert result.volume_version_id == 1
    assert result.source_volume_version_id == source
    assert result.version == 1
    assert set(output.commits[0]) == {
        "model.safetensors",
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "chalk-weight-version.json",
        "chalk-weight-source.json",
    }
    snapshot = output.at_version(result.volume_version_id)
    output.put_file("config.json", b"mutable future")
    assert json.loads(snapshot.read_file("config.json"))["model_type"] == "qwen3"
    assert (
        json.loads(snapshot.read_file("chalk-weight-source.json"))["volume_version_id"]
        == source
    )
    materialized = stream.materialize(
        1, tmp_path / "immutable", source_volume_version_id=source
    )
    assert torch.equal(
        load_file(str(materialized / "model.safetensors"))["w"], torch.ones(10)
    )


def test_explicit_snapshot_zero_never_falls_back_to_main(stream, tmp_path):
    publisher(stream).publish({"w": torch.zeros(10)})
    from chalkcompute._volume import VolumeFileNotFoundError

    with pytest.raises(VolumeFileNotFoundError):
        stream.materialize(0, tmp_path / "zero", source_volume_version_id=0)
    assert not (tmp_path / "zero").exists()


def test_immutable_source_corrupt_payload_does_not_write_output(stream, monkeypatch):
    _, source = seeded_with_tokenizer(stream, monkeypatch)
    stream.volume.snapshots[source]["v000001/patches.bin"] = b"corrupt"
    output = output_volume()
    with pytest.raises(WeightStreamError, match="Checksum"):
        stream.materialize_to_volume(
            1, source_volume_version_id=source, destination=output
        )
    assert output.commits == []


def test_output_requires_full_tokenizer_and_config(stream, monkeypatch):
    pub = publisher(stream)
    pub.publish({"w": torch.zeros(10)})
    output = output_volume()
    with pytest.raises(WeightStreamError, match="tokenizer"):
        stream.materialize_to_volume(0, source_volume_version_id=1, destination=output)
    assert output.commits == []
    stream.volume.snapshots[1]["v000000/config.json"] = b"{}"
    with pytest.raises(WeightStreamError, match="architecture"):
        stream.materialize_to_volume(0, source_volume_version_id=1, destination=output)
    assert output.commits == []


def test_output_cannot_overwrite_source_or_existing_checkpoint(stream, monkeypatch):
    _, source = seeded_with_tokenizer(stream, monkeypatch)
    with pytest.raises(WeightStreamError, match="different volume"):
        stream.materialize_to_volume(
            1, source_volume_version_id=source, destination=stream.volume
        )
    output = output_volume()
    output.put_file("existing", b"data")
    with pytest.raises(WeightStreamError, match="empty"):
        stream.materialize_to_volume(
            1, source_volume_version_id=source, destination=output
        )
    assert output.read_file("existing") == b"data"


def test_explicit_snapshot_zero_with_version_zero_is_used(stream, monkeypatch):
    _, source = seeded_with_tokenizer(stream, monkeypatch)
    stream.volume.snapshots[0] = dict(stream.volume.snapshots[source])
    output = output_volume()
    result = stream.materialize_to_volume(
        0, source_volume_version_id=0, destination=output
    )
    assert result.source_volume_version_id == result.version == 0
    assert json.loads(output.read_file("chalk-weight-version.json"))["version"] == 0


def test_immutable_source_missing_ancestor_fails_without_output(stream, monkeypatch):
    _, source = seeded_with_tokenizer(stream, monkeypatch)
    del stream.volume.snapshots[source]["v000000/manifest.json"]
    output = output_volume()
    from chalkcompute._volume import VolumeFileNotFoundError

    with pytest.raises(VolumeFileNotFoundError):
        stream.materialize_to_volume(
            1, source_volume_version_id=source, destination=output
        )
    assert output.commits == []


@pytest.mark.parametrize("snapshot", [None, True, -1, "1"])
def test_output_requires_explicit_valid_source_snapshot(stream, snapshot):
    output = output_volume()
    with pytest.raises(ValueError, match="source_volume_version_id"):
        stream.materialize_to_volume(
            0, source_volume_version_id=snapshot, destination=output
        )
    assert output.commits == []


def test_output_commit_failure_never_changes_source(stream, monkeypatch):
    _, source = seeded_with_tokenizer(stream, monkeypatch)
    before = list(stream.volume.commits)
    output = output_volume()
    output.fail_commit = True
    with pytest.raises(RuntimeError, match="injected"):
        stream.materialize_to_volume(
            1, source_volume_version_id=source, destination=output
        )
    assert output.commits == []
    assert stream.volume.commits == before


def test_materialize_reads_shared_patch_blob_once_per_manifest(
    stream, tmp_path, monkeypatch
):
    from collections import Counter

    pub = publisher(stream)
    pub.publish({"a": torch.zeros(100), "b": torch.zeros(100)})
    pub.publish({"a": torch.ones(100), "b": torch.full((100,), 2.0)})
    source = stream.volume.version.id
    at_version = stream.volume.at_version
    reads = Counter()

    def counted_snapshot(version):
        snapshot = at_version(version)
        read_file = snapshot.read_file

        def counted_read(path):
            reads[path] += 1
            return read_file(path)

        snapshot.read_file = counted_read
        return snapshot

    monkeypatch.setattr(stream.volume, "at_version", counted_snapshot)
    folder = stream.materialize(1, tmp_path / "shared", source_volume_version_id=source)
    assert reads["v000001/patches.bin"] == 1
    actual = load_file(str(folder / "model.safetensors"))
    assert torch.equal(actual["a"], torch.ones(100))
    assert torch.equal(actual["b"], torch.full((100,), 2.0))
