from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from chalkcompute._gen.chalk.weightstream.v1 import service_pb as pb
from chalkcompute._gen.google.rpc.status_pb import Status
from chalkcompute._live_policy import LivePolicy
from chalkcompute._weight_promotion import (
    WeightPromotionCancelled,
    WeightPromotionFailed,
    WeightStreamPromotion,
)
from chalkcompute.weights import WeightStream, WeightStreamError
from tests._fakes import MemoryWeightVolume, StubbedConnectClient


State = pb.WeightStreamPromotionState


def promotion(state=State.QUEUED, *, cleanup=False):
    result = None
    if state == State.SUCCEEDED:
        result = Oneof(
            "result",
            pb.WeightStreamPromotionResult(
                model_name="target",
                model_version_id="registry-row",
                model_version=1,
                model_volume_name="checkpoint-output",
                model_volume_version_id=0,
            ),
        )
    elif state == State.FAILED:
        result = Oneof("error", Status(code=13, message="materialization failed"))
    return pb.WeightStreamPromotion(
        id="operation",
        weight_stream_id="stream",
        source_volume_name="weights-test",
        source_version=pb.WeightStreamVersion(
            weight_stream_id="stream",
            version=0,
            volume_version_id=0,
            architecture_hash="hash",
            kind=pb.WeightStreamVersionKind.KEYFRAME,
        ),
        model_name="target",
        aliases=["candidate"],
        state=state,
        outcome=result,
        completed_at=Timestamp(seconds=1)
        if state in {State.SUCCEEDED, State.FAILED, State.CANCELLED}
        else None,
        cleanup_pending=cleanup,
    )


class PromotionClient(StubbedConnectClient):
    def __init__(self):
        self.reply = promotion()
        self.cancel_reply = promotion(State.CANCELLING)
        self.error = None
        self.lose_start_once = False
        self.keys = {}
        self.requests = []
        self.timeouts = []

    def call(self, method, request, *, timeout=30):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error:
            raise self.error
        if method.name == "start_weight_stream_promotion":
            self.keys.setdefault(request.idempotency_key, self.reply.id)
            if self.lose_start_once:
                self.lose_start_once = False
                raise ConnectError(Code.UNAVAILABLE, "response lost")
            return pb.StartWeightStreamPromotionResponse(promotion=self.reply)
        if method.name == "get_weight_stream_promotion":
            return pb.GetWeightStreamPromotionResponse(promotion=self.reply)
        if method.name == "cancel_weight_stream_promotion":
            return pb.CancelWeightStreamPromotionResponse(promotion=self.cancel_reply)
        raise AssertionError(method.name)


def stream(client):
    value = WeightStream("test", "model", volume=cast(Any, MemoryWeightVolume()))
    value._metadata = cast(Any, SimpleNamespace(client=client, id="stream"))
    return value


def start(client):
    return stream(client).promote(
        0, "target", idempotency_key="durable-retry", aliases=["candidate"]
    )


def test_start_and_get_preserve_zero_source_and_result_versions():
    client = PromotionClient()
    operation = start(client)
    request = client.requests[0]
    assert isinstance(request, pb.StartWeightStreamPromotionRequest)
    assert request.version == 0
    assert request.idempotency_key == "durable-retry"
    assert operation.snapshot.source_version.volume_version_id == 0
    assert operation.state == "QUEUED"
    assert operation.result is None and not operation.done
    detached = operation.snapshot
    detached.source_volume_name = "mutated"
    assert operation.snapshot.source_volume_name == "weights-test"
    client.reply = promotion(State.SUCCEEDED)
    recovered = WeightStreamPromotion.get("operation", client=cast(Any, client))
    result = recovered.wait(timeout=0)
    assert result.model_version == 1
    assert result.model_volume_version_id == 0
    assert result.model_volume_name == "checkpoint-output"
    assert result.model_version_id == "registry-row"


def test_start_response_loss_requires_explicit_same_key_retry():
    client = PromotionClient()
    client.lose_start_once = True
    with pytest.raises(ConnectError):
        start(client)
    assert len(client.requests) == 1
    assert client.keys == {"durable-retry": "operation"}
    operation = start(client)
    assert operation.id == "operation"
    assert len(client.requests) == 2
    assert {request.idempotency_key for request in client.requests} == {"durable-retry"}


@pytest.mark.parametrize("version", [True, -1, 2**64, "0", 0.0])
def test_invalid_version_refused_before_rpc(version):
    client = PromotionClient()
    with pytest.raises(ValueError, match="uint64"):
        stream(client).promote(version, "target", idempotency_key="key")
    assert not client.requests


@pytest.mark.parametrize(
    "kwargs",
    [
        {"idempotency_key": ""},
        {"idempotency_key": " "},
        {"idempotency_key": "key", "aliases": "candidate"},
        {"idempotency_key": "key", "aliases": [""]},
        {"idempotency_key": "key", "aliases": ["same", "same"]},
    ],
)
def test_invalid_retry_key_or_aliases_refused_before_rpc(kwargs):
    client = PromotionClient()
    with pytest.raises(ValueError):
        stream(client).promote(0, "target", **kwargs)
    assert not client.requests


def test_unsupported_backend_preserves_original_error_and_never_falls_back():
    client = PromotionClient()
    error = ConnectError(Code.UNIMPLEMENTED, "no compatible promotion handler")
    client.error = error
    with pytest.raises(ConnectError) as caught:
        start(client)
    assert caught.value is error
    assert len(client.requests) == 1
    assert isinstance(client.requests[0], pb.StartWeightStreamPromotionRequest)


def test_cancel_registry_commit_race_returns_registered_result():
    client = PromotionClient()
    operation = start(client)
    client.cancel_reply = promotion(State.SUCCEEDED)
    assert operation.cancel() is operation
    assert operation.wait(timeout=0).model_volume_name == "checkpoint-output"
    assert operation.state == "SUCCEEDED"
    assert isinstance(client.requests[-1], pb.CancelWeightStreamPromotionRequest)
    assert len(client.requests) == 2


def test_cancelled_cleanup_pending_is_preserved_until_refresh():
    client = PromotionClient()
    operation = start(client)
    client.cancel_reply = promotion(State.CANCELLED, cleanup=True)
    operation.cancel()
    assert operation.done and operation.cleanup_pending
    with pytest.raises(WeightPromotionCancelled) as caught:
        operation.wait(timeout=0)
    assert caught.value.operation is operation
    assert caught.value.operation.cleanup_pending
    client.reply = promotion(State.CANCELLED, cleanup=False)
    operation.refresh()
    assert not operation.cleanup_pending


def test_failed_error_keeps_code_handle_and_cleanup_state():
    client = PromotionClient()
    client.reply = promotion(State.FAILED, cleanup=True)
    operation = start(client)
    with pytest.raises(WeightPromotionFailed, match="materialization failed") as caught:
        operation.wait(timeout=0)
    assert caught.value.code == 13
    assert caught.value.operation is operation
    assert operation.cleanup_pending


def test_wait_timeout_leaves_existing_job_and_caps_rpc_deadline(monkeypatch):
    import chalkcompute._weight_promotion as module

    now = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    client = PromotionClient()
    operation = start(client)
    with pytest.raises(TimeoutError, match="existing operation ID"):
        operation.wait(timeout=2, poll_interval=1)
    assert [type(request) for request in client.requests] == [
        pb.StartWeightStreamPromotionRequest,
        pb.GetWeightStreamPromotionRequest,
        pb.GetWeightStreamPromotionRequest,
    ]
    assert client.timeouts[1:] == [2, 1]
    assert operation.id == "operation" and not operation.done
    client.reply = promotion(State.SUCCEEDED)
    assert operation.wait(timeout=1).model_version_id == "registry-row"


@pytest.mark.parametrize(
    "state,change",
    [
        (State.SUCCEEDED, {"outcome": None}),
        (State.FAILED, {"outcome": None}),
        (State.FAILED, {"outcome": Oneof("error", Status(code=0))}),
        (State.CANCELLED, {"outcome": promotion(State.SUCCEEDED).outcome}),
        (State.MATERIALIZING, {"outcome": promotion(State.SUCCEEDED).outcome}),
        (State.REGISTERING, {"completed_at": Timestamp(seconds=1)}),
        (State.SUCCEEDED, {"completed_at": None}),
        (State.SUCCEEDED, {"cleanup_pending": True}),
        (State.QUEUED, {"cleanup_pending": True}),
        (State.UNSPECIFIED, {}),
        (State.QUEUED, {"source_version": None}),
    ],
)
def test_inconsistent_state_outcome_rejected(state, change):
    client = PromotionClient()
    item = promotion(state)
    for name, value in change.items():
        setattr(item, name, value)
    client.reply = item
    with pytest.raises(WeightStreamError):
        start(client)


def test_refresh_cannot_change_frozen_source_or_terminal_result():
    client = PromotionClient()
    operation = start(client)
    client.reply = promotion()
    client.reply.source_version.volume_version_id = 1
    with pytest.raises(WeightStreamError, match="identity changed"):
        operation.refresh()
    assert operation.snapshot.source_version.volume_version_id == 0
    client.reply = promotion(State.SUCCEEDED)
    operation.refresh()
    client.reply.outcome.value.model_volume_name = "other-output"
    with pytest.raises(WeightStreamError, match="outcome changed"):
        operation.refresh()
    assert operation.result.model_volume_name == "checkpoint-output"


def test_live_policy_promote_uses_durable_operation_entrypoint():
    client = PromotionClient()
    policy = LivePolicy(stream(client), cast(Any, SimpleNamespace()), cast(Any, client))
    operation = policy.promote(
        0, "target", idempotency_key="durable-retry", aliases=["candidate"]
    )
    assert operation.id == "operation"
    assert isinstance(client.requests[0], pb.StartWeightStreamPromotionRequest)


@pytest.mark.parametrize(
    "change",
    [
        {"model_version_id": ""},
        {"model_volume_name": ""},
        {"model_name": "other"},
        {"model_version": 0},
    ],
)
def test_success_requires_matching_complete_registry_artifact_identity(change):
    client = PromotionClient()
    client.reply = promotion(State.SUCCEEDED)
    for name, value in change.items():
        setattr(client.reply.outcome.value, name, value)
    with pytest.raises(WeightStreamError, match="registry/artifact identity"):
        start(client)


def test_get_and_refresh_reject_wrong_identity_without_mutating_handle():
    client = PromotionClient()
    with pytest.raises(WeightStreamError, match="different operation ID"):
        WeightStreamPromotion.get("wrong-operation", client=cast(Any, client))
    operation = start(client)
    client.reply = promotion()
    client.reply.id = "other-operation"
    with pytest.raises(WeightStreamError, match="identity changed"):
        operation.refresh()
    assert operation.id == "operation"


def test_terminal_cleanup_cannot_regress_or_change_completion():
    client = PromotionClient()
    client.reply = promotion(State.CANCELLED)
    operation = start(client)
    client.reply = promotion(State.CANCELLED, cleanup=True)
    with pytest.raises(WeightStreamError, match="Terminal"):
        operation.refresh()
    client.reply = promotion(State.CANCELLED)
    client.reply.completed_at = Timestamp(seconds=2)
    with pytest.raises(WeightStreamError, match="Terminal"):
        operation.refresh()
    assert not operation.cleanup_pending


def test_unregistered_stream_requires_registration_before_any_start():
    client = PromotionClient()
    value = stream(client)
    value._metadata = None
    with pytest.raises(WeightStreamError, match="Register"):
        value.promote(0, "target", idempotency_key="key")
    assert not client.requests


def test_transport_error_does_not_restart_or_cancel_existing_operation():
    client = PromotionClient()
    operation = start(client)
    error = ConnectError(Code.UNAVAILABLE, "temporary transport failure")
    client.error = error
    with pytest.raises(ConnectError) as caught:
        operation.wait(timeout=1)
    assert caught.value is error
    assert operation.id == "operation" and operation.state == "QUEUED"
    assert len(client.requests) == 2
    assert isinstance(client.requests[-1], pb.GetWeightStreamPromotionRequest)


def test_source_kind_must_be_specified_even_for_version_zero():
    client = PromotionClient()
    client.reply.source_version.kind = pb.WeightStreamVersionKind.UNSPECIFIED
    with pytest.raises(WeightStreamError, match="version kind"):
        start(client)


def test_registry_implicit_latest_cannot_change_frozen_requested_aliases():
    client = PromotionClient()
    operation = start(client)
    client.reply = promotion(State.SUCCEEDED)
    client.reply.aliases.append("latest")
    with pytest.raises(WeightStreamError, match="identity changed"):
        operation.refresh()
    assert operation.snapshot.aliases == ["candidate"]
