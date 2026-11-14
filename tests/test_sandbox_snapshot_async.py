from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from connectrpc.method import IdempotencyLevel, MethodInfo
from connectrpc.request import Headers, RequestContext
from protobuf.wkt import Timestamp

from chalkcompute import (
    SandboxSnapshot,
    SandboxSnapshotError,
    SandboxSnapshotTimeoutError,
)
from chalkcompute import _sandbox
from chalkcompute._gen.chalk.sandbox.v2 import snapshot_service_pb as pb
from chalkcompute._rpc import RetryInterceptor
from chalkcompute._sandbox import SandboxClient


@pytest.fixture
def snapshot_client():
    with SandboxClient() as client:
        stub = MagicMock()
        stub.start_sandbox_snapshot.return_value = pb.StartSandboxSnapshotResponse(
            snapshot=pb.SandboxSnapshot(
                id="snapshot-1",
                sandbox_id="sandbox-1",
                status=pb.SandboxSnapshotStatus.PENDING,
            )
        )
        client._snapshot_stub = stub
        yield client, stub


def test_submission_returns_pending_without_waiting(snapshot_client):
    client, stub = snapshot_client
    snapshot = client.get("sandbox-1").snapshot(wait=False, idempotency_key="capture-1")
    assert snapshot.status == "pending"
    assert snapshot.id == "snapshot-1"
    request = stub.start_sandbox_snapshot.call_args.args[0]
    assert request.sandbox_id == "sandbox-1"
    assert request.idempotency_key == "capture-1"
    assert stub.start_sandbox_snapshot.call_args.kwargs["timeout_ms"] == 10_000
    stub.get_sandbox_snapshot.assert_not_called()
    stub.create_sandbox_snapshot.assert_not_called()


def test_lost_submission_can_be_recovered_with_same_key(snapshot_client):
    client, stub = snapshot_client
    response = stub.start_sandbox_snapshot.return_value
    stub.start_sandbox_snapshot.side_effect = [
        ConnectError(Code.DEADLINE_EXCEEDED, "response lost"),
        response,
    ]
    sandbox = client.get("sandbox-1")
    with pytest.raises(SandboxSnapshotError) as caught:
        sandbox.snapshot(wait=False)
    key = caught.value.idempotency_key
    assert key
    snapshot = sandbox.snapshot(wait=False, idempotency_key=key)
    assert snapshot.id == "snapshot-1"
    assert [
        call.args[0].idempotency_key
        for call in stub.start_sandbox_snapshot.call_args_list
    ] == [key, key]


def test_unimplemented_never_falls_back_to_sync(snapshot_client):
    client, stub = snapshot_client
    stub.start_sandbox_snapshot.side_effect = ConnectError(
        Code.UNIMPLEMENTED, "unknown method"
    )
    with pytest.raises(
        SandboxSnapshotError, match="does not support asynchronous"
    ) as caught:
        client.get("sandbox-1").snapshot(wait=False)
    assert caught.value.code == Code.UNIMPLEMENTED
    stub.create_sandbox_snapshot.assert_not_called()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": -1},
        {"timeout": float("nan")},
        {"poll_interval": 0},
        {"idempotency_key": ""},
        {"idempotency_key": "   "},
        {"idempotency_key": "é" * 65},
    ],
)
def test_invalid_arguments_never_submit(snapshot_client, kwargs):
    client, stub = snapshot_client
    with pytest.raises(ValueError):
        client.get("sandbox-1").snapshot(**kwargs)
    stub.start_sandbox_snapshot.assert_not_called()


def test_wait_timeout_retains_handle_and_can_reconnect(snapshot_client):
    client, stub = snapshot_client
    with pytest.raises(SandboxSnapshotTimeoutError) as caught:
        client.get("sandbox-1").snapshot(timeout=0)
    snapshot = caught.value.snapshot
    assert caught.value.snapshot_id == snapshot.id == "snapshot-1"
    stub.get_sandbox_snapshot.assert_not_called()
    stub.get_sandbox_snapshot.return_value = pb.GetSandboxSnapshotResponse(
        snapshot=pb.SandboxSnapshot(
            id=snapshot.id,
            sandbox_id="sandbox-1",
            status=pb.SandboxSnapshotStatus.COMPLETED,
        )
    )
    reconnected = SandboxSnapshot.from_id(snapshot.id, client=client)
    assert reconnected.wait(timeout=0).status == "completed"
    assert stub.start_sandbox_snapshot.call_count == 1


def test_wait_status_requests_share_remaining_budget(snapshot_client, monkeypatch):
    client, stub = snapshot_client
    snapshot = client.get("sandbox-1").snapshot(wait=False)
    now = [0.0]
    monkeypatch.setattr(_sandbox.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        _sandbox.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay)
    )

    def unavailable(request, *, timeout_ms, **kwargs):
        now[0] += timeout_ms / 1000
        raise ConnectError(Code.DEADLINE_EXCEEDED, "temporary read failure")

    stub.get_sandbox_snapshot.side_effect = unavailable
    with pytest.raises(SandboxSnapshotTimeoutError):
        snapshot.wait(timeout=0.25)
    assert now[0] == 0.25
    assert stub.get_sandbox_snapshot.call_args.kwargs["timeout_ms"] == 250
    assert stub.start_sandbox_snapshot.call_count == 1


def test_wait_recovers_transient_status_read_and_updates_progress(
    snapshot_client, monkeypatch
):
    client, stub = snapshot_client
    snapshot = client.get("sandbox-1").snapshot(wait=False)
    completed = pb.SandboxSnapshot(
        id=snapshot.id,
        sandbox_id="sandbox-1",
        status=pb.SandboxSnapshotStatus.COMPLETED,
        progress=pb.SandboxSnapshotProgress(
            phase=pb.SandboxSnapshotPhase.COMPLETED,
            bytes_completed=123,
            bytes_total=123,
            updated_at=Timestamp(seconds=5),
        ),
    )
    stub.get_sandbox_snapshot.side_effect = [
        ConnectError(Code.UNAVAILABLE, "rollout"),
        pb.GetSandboxSnapshotResponse(snapshot=completed),
    ]
    monkeypatch.setattr(_sandbox.time, "sleep", lambda _: None)
    assert snapshot.wait() is snapshot
    assert snapshot.phase == "completed"
    assert snapshot.bytes_completed == snapshot.bytes_total == 123
    assert snapshot.progress_updated_at == "1970-01-01T00:00:05+00:00"
    assert stub.start_sandbox_snapshot.call_count == 1


def test_recovery_required_is_reported_without_restarting_capture(snapshot_client):
    client, stub = snapshot_client
    stub.start_sandbox_snapshot.return_value = pb.StartSandboxSnapshotResponse(
        snapshot=pb.SandboxSnapshot(
            id="snapshot-1",
            sandbox_id="sandbox-1",
            status=pb.SandboxSnapshotStatus.IN_PROGRESS,
            status_message="host restarted during GPU checkpoint",
            progress=pb.SandboxSnapshotProgress(
                phase=pb.SandboxSnapshotPhase.RECOVERY_REQUIRED
            ),
        )
    )
    with pytest.raises(SandboxSnapshotError, match="recovery_required"):
        client.get("sandbox-1").snapshot()
    stub.get_sandbox_snapshot.assert_not_called()
    assert stub.start_sandbox_snapshot.call_count == 1


def test_transport_retries_exact_same_idempotent_start_request(
    snapshot_client, monkeypatch
):
    client, stub = snapshot_client
    response = stub.start_sandbox_snapshot.return_value
    observed = []
    monkeypatch.setattr(_sandbox.time, "sleep", lambda _: None)
    context = RequestContext(
        method=MethodInfo(
            name="StartSandboxSnapshot",
            service_name="chalk.sandbox.v2.SandboxSnapshotService",
            input=pb.StartSandboxSnapshotRequest,
            output=pb.StartSandboxSnapshotResponse,
            idempotency_level=IdempotencyLevel.UNKNOWN,
        ),
        http_method="POST",
        request_headers=Headers(),
    )

    def send(request, context):
        observed.append(request)
        if len(observed) == 1:
            raise ConnectError(Code.UNAVAILABLE, "ack lost")
        return response

    def intercepted(request, **kwargs):
        return RetryInterceptor().intercept_unary_sync(send, request, context)

    stub.start_sandbox_snapshot.side_effect = intercepted
    client.get("sandbox-1").snapshot(wait=False, idempotency_key="one-capture")
    assert len(observed) == 2
    assert observed[0] is observed[1]
    assert observed[0].idempotency_key == "one-capture"
