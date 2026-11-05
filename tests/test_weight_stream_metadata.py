from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from chalkcompute._gen.chalk.weightstream.v1 import service_pb as pb
from chalkcompute._weights_format import pointer_json
from chalkcompute.weights import WeightMetadataPending, WeightStream, WeightStreamError
from tests._fakes import MemoryWeightVolume, StubbedConnectClient


torch = pytest.importorskip("torch")
pytest.importorskip("safetensors")
pytest.importorskip("zstandard")
pytest.importorskip("xxhash")


class MetadataClient(StubbedConnectClient):
    def __init__(self):
        self.records = {}
        self.attempts = []
        self.requests = []
        self.fail_record = False
        self.fail_create = False
        self.fail_after_record = False

    def call(self, method, request, **kwargs):
        self.requests.append(request)
        if method.name == "create_weight_stream":
            if self.fail_create:
                raise RuntimeError("backend unavailable")
            return pb.CreateWeightStreamResponse(
                weight_stream=pb.WeightStream(
                    id="stream-id",
                    name="test",
                    volume_name="weights-test",
                    base_model="test/model",
                )
            )
        if method.name == "list_weight_stream_versions":
            return pb.ListWeightStreamVersionsResponse(
                versions=list(self.records.values())
            )
        if method.name == "record_weight_stream_version":
            descriptor = request.version
            self.attempts.append(descriptor)
            if self.fail_record:
                raise RuntimeError("backend unavailable")
            previous = self.records.get(descriptor.version)
            if previous is not None:
                assert descriptor == previous
            self.records[descriptor.version] = descriptor
            if self.fail_after_record:
                raise RuntimeError("response lost")
            return pb.RecordWeightStreamVersionResponse(version=descriptor)
        return SimpleNamespace()


def new_stream():
    return WeightStream("test", "test/model", volume=cast(Any, MemoryWeightVolume()))


def test_metadata_outage_after_commit_advances_state_and_reconciles():
    stream = new_stream()
    client = MetadataClient()
    stream.register(client=cast(Any, client))
    pub = stream.publisher(config={"hidden_size": 10})
    client.fail_record = True
    with pytest.raises(WeightMetadataPending) as caught:
        pub.publish({"w": torch.zeros(10)})
    assert pub.last_publish_stats["status"] == "metadata_pending"
    assert pub.last_publish_stats["volume_version_id"] == 1
    assert pub.last_publish_stats["version"] == 0
    assert pub.last_publish_stats["phases_seconds"]["metadata"] >= 0
    assert caught.value.version == 0
    assert caught.value.volume_version_id == 1
    assert "publication succeeded" in str(caught.value).lower()
    assert stream.current() == 0
    with pytest.raises(WeightMetadataPending) as second:
        pub.publish({"w": torch.ones(10)})
    assert second.value.version == 1
    assert stream.current() == 1
    client.fail_record = False
    assert stream.reconcile() == [1, 0]
    assert stream.reconcile() == []
    assert client.records[0].volume_version_id == 1
    assert client.records[1].volume_version_id == 2
    assert pub.publish({"w": torch.full((10,), 2.0)}) == 2


def test_register_existing_volume_chain_after_pin_and_prune():
    stream = new_stream()
    pub = stream.publisher(config={"hidden_size": 10}, keyframe_every=2)
    for value in range(5):
        pub.publish({"w": torch.full((10,), float(value))})
    stream.pin(3)
    stream.unpin()
    stream.prune(keep_keyframes=1)
    assert len(stream.versions()) == 1
    reopened = WeightStream("test", volume=stream.volume)
    client = MetadataClient()
    reopened.register(client=cast(Any, client))
    assert reopened.base_model == "test/model"
    assert reopened.id == "stream-id"
    assert [item.version for item in client.attempts] == [4, 3, 2, 1, 0]
    assert [client.records[v].volume_version_id for v in range(5)] == [1, 2, 3, 4, 5]
    assert reopened.current() == 4
    assert reopened.reconcile() == []


def test_response_lost_after_success_is_idempotent():
    stream = new_stream()
    client = MetadataClient()
    stream.register(client=cast(Any, client))
    client.fail_after_record = True
    with pytest.raises(WeightMetadataPending):
        stream.publisher(config={"hidden_size": 10}).publish({"w": torch.zeros(10)})
    client.fail_after_record = False
    assert stream.reconcile() == []
    assert len(client.attempts) == 1


def test_metadata_registration_failure_leaves_volume_publication_intact():
    stream = new_stream()
    stream.publisher(config={"hidden_size": 10}).publish({"w": torch.zeros(10)})
    client = MetadataClient()
    client.fail_create = True
    with pytest.raises(WeightStreamError, match="publications remain committed"):
        stream.register(client=cast(Any, client))
    assert stream.id is None
    assert stream.current() == 0
    client.fail_create = False
    stream.register(client=cast(Any, client))
    assert client.records[0].volume_version_id == 1


def test_reconciliation_bound_and_missing_history_fail_clearly(monkeypatch):
    stream = new_stream()
    pub = stream.publisher(config={"hidden_size": 10})
    pub.publish({"w": torch.zeros(10)})
    pub.publish({"w": torch.ones(10)})
    client = MetadataClient()
    with pytest.raises(WeightStreamError, match="max_snapshots"):
        stream.register(client=cast(Any, client), max_snapshots=1)
    assert client.records == {}
    original = stream.volume.iter_versions
    monkeypatch.setattr(
        stream.volume, "iter_versions", lambda **kwargs: iter(list(original())[2:])
    )
    with pytest.raises(WeightStreamError, match="incomplete"):
        stream.reconcile()
    assert client.records == {}


def test_registered_pin_uses_control_plane_and_volume_only_stays_explicit():
    stream = new_stream()
    assert stream.id is None
    client = MetadataClient()
    stream.register(client=cast(Any, client))
    stream.pin(0)
    stream.unpin()
    assert isinstance(client.requests[-2], pb.SetWeightStreamPinRequest)
    assert client.requests[-2].version == 0
    assert isinstance(client.requests[-1], pb.ClearWeightStreamPinRequest)
    assert stream.volume.commits == []


def test_original_commit_snapshot_survives_concurrent_pin(monkeypatch):
    stream = new_stream()
    client = MetadataClient()
    stream.register(client=cast(Any, client))
    original = stream.current
    calls = 0

    def current_with_pin_race():
        nonlocal calls
        calls += 1
        if calls == 3:
            stream.volume.put_file("PIN", pointer_json(0))
        return original()

    monkeypatch.setattr(stream, "current", current_with_pin_race)
    stream.publisher(config={"hidden_size": 10}).publish({"w": torch.zeros(10)})
    assert stream.volume.version.id == 2
    assert client.records[0].volume_version_id == 1
    assert stream.reconcile() == []
    assert stream.reconciliation_stats["history_snapshots"] == 3


def test_reopened_registered_stream_uses_existing_identity():
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    stream = new_stream()
    client = MetadataClient()
    original = client.call

    def existing(method, request, **kwargs):
        if method.name == "create_weight_stream":
            raise ConnectError(Code.ALREADY_EXISTS, "existing")
        if method.name == "get_weight_stream":
            assert request.weight_stream_key.value == "test"
            return pb.GetWeightStreamResponse(
                weight_stream=pb.WeightStream(
                    id="existing-id",
                    volume_name="weights-test",
                    base_model="test/model",
                )
            )
        return original(method, request, **kwargs)

    client.call = existing
    stream.register(client=cast(Any, client))
    assert stream.id == "existing-id"


def test_reconciliation_refuses_ambiguous_reused_version_history():
    stream = new_stream()
    pub = stream.publisher(config={"hidden_size": 10})
    pub.publish({"w": torch.zeros(10)})
    pub.publish({"w": torch.ones(10)})
    stream.volume.put_file("CURRENT", pointer_json(0))
    client = MetadataClient()
    with pytest.raises(WeightStreamError, match="Ambiguous"):
        stream.register(client=cast(Any, client))
    assert client.records == {}
