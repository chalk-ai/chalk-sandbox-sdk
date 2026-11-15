from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from connectrpc.method import IdempotencyLevel, MethodInfo
from connectrpc.request import Headers, RequestContext

from chalkcompute import (
    SandboxCreationError,
    SandboxSnapshot,
    SandboxSnapshotError,
    SandboxSnapshotTimeoutError,
)
from chalkcompute import _sandbox
from chalkcompute._gen.chalk.sandbox.v2 import snapshot_service_pb as pb
from chalkcompute._rpc import RetryInterceptor
from chalkcompute._sandbox import SandboxClient
from chalkcompute._gen.chalk.sandbox.v2 import service_pb as sandbox_pb


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
        stub.get_sandbox_snapshot.return_value = pb.GetSandboxSnapshotResponse(
            snapshot=pb.SandboxSnapshot(
                id="snapshot-1",
                sandbox_id="sandbox-1",
                status=pb.SandboxSnapshotStatus.COMPLETED,
            )
        )
        client._snapshot_stub = stub
        yield client, stub


def test_snapshot_returns_only_after_publication(snapshot_client, monkeypatch):
    client, stub = snapshot_client
    monkeypatch.setattr(_sandbox.time, "sleep", lambda _: None)
    stub.get_sandbox_snapshot.side_effect = [
        pb.GetSandboxSnapshotResponse(
            snapshot=pb.SandboxSnapshot(
                id="snapshot-1",
                sandbox_id="sandbox-1",
                status=pb.SandboxSnapshotStatus.IN_PROGRESS,
                progress=pb.SandboxSnapshotProgress(
                    phase=pb.SandboxSnapshotPhase.UPLOADING,
                    bytes_completed=123,
                    bytes_total=123,
                ),
            )
        ),
        stub.get_sandbox_snapshot.return_value,
    ]
    snapshot = client.get("sandbox-1").snapshot()
    assert snapshot.status == "completed"
    assert snapshot.id == "snapshot-1"
    request = stub.start_sandbox_snapshot.call_args.args[0]
    assert request.sandbox_id == "sandbox-1"
    assert request.idempotency_key
    assert stub.start_sandbox_snapshot.call_args.kwargs["timeout_ms"] == 10_000
    assert stub.get_sandbox_snapshot.call_count == 2
    stub.create_sandbox_snapshot.assert_not_called()


def test_separate_snapshot_calls_start_distinct_captures(snapshot_client):
    client, stub = snapshot_client
    sandbox = client.get("sandbox-1")
    sandbox.snapshot()
    sandbox.snapshot()
    keys = [
        call.args[0].idempotency_key
        for call in stub.start_sandbox_snapshot.call_args_list
    ]
    assert all(keys)
    assert len(set(keys)) == 2


def test_submission_reports_server_error(snapshot_client):
    client, stub = snapshot_client
    stub.start_sandbox_snapshot.side_effect = ConnectError(
        Code.FAILED_PRECONDITION, "sandbox is not running"
    )
    with pytest.raises(SandboxSnapshotError, match="sandbox is not running") as caught:
        client.get("sandbox-1").snapshot()
    assert caught.value.code == Code.FAILED_PRECONDITION
    stub.create_sandbox_snapshot.assert_not_called()


def test_capture_has_no_public_timeout():
    assert list(inspect.signature(_sandbox.Sandbox.snapshot).parameters) == ["self"]


def test_capture_waits_beyond_former_timeout(snapshot_client, monkeypatch):
    client, stub = snapshot_client
    now = [0.0]
    monkeypatch.setattr(_sandbox.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        _sandbox.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay)
    )
    completed = stub.get_sandbox_snapshot.return_value

    def status(request, *, timeout_ms, **kwargs):
        assert timeout_ms == 10_000
        now[0] += timeout_ms / 1000
        if now[0] <= 600:
            raise ConnectError(Code.DEADLINE_EXCEEDED, "temporary read timeout")
        return completed

    stub.get_sandbox_snapshot.side_effect = status
    assert client.get("sandbox-1").snapshot().status == "completed"
    assert now[0] > 600
    assert stub.start_sandbox_snapshot.call_count == 1


def test_pending_snapshot_loaded_by_id_waits_before_restore(
    snapshot_client, monkeypatch
):
    client, stub = snapshot_client
    monkeypatch.setattr(_sandbox.time, "sleep", lambda _: None)
    stub.get_sandbox_snapshot.side_effect = [
        pb.GetSandboxSnapshotResponse(
            snapshot=stub.start_sandbox_snapshot.return_value.snapshot
        ),
        stub.get_sandbox_snapshot.return_value,
    ]
    sandbox_stub = MagicMock()
    sandbox_stub.create_sandbox.return_value = sandbox_pb.CreateSandboxResponse(
        sandbox=sandbox_pb.SandboxInfo(
            id="restored-1", status=sandbox_pb.SandboxStatus.PENDING
        )
    )
    sandbox_stub.get_sandbox.return_value = sandbox_pb.GetSandboxResponse(
        sandbox=sandbox_pb.SandboxInfo(
            id="restored-1", status=sandbox_pb.SandboxStatus.RUNNING
        )
    )
    client._stub = sandbox_stub
    saved = SandboxSnapshot.from_id("snapshot-1", client=client)
    assert saved.restore().id == "restored-1"
    stub.start_sandbox_snapshot.assert_not_called()
    assert sandbox_stub.create_sandbox.call_count == 1
    assert sandbox_stub.get_sandbox.call_count == 1


def test_restore_timeout_still_bounds_pending_capture(snapshot_client, monkeypatch):
    client, stub = snapshot_client
    now = [0.0]
    monkeypatch.setattr(_sandbox.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        _sandbox.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay)
    )
    saved = _sandbox._pb_to_sandbox_snapshot(
        stub.start_sandbox_snapshot.return_value.snapshot, client=client
    )

    def unavailable(request, *, timeout_ms, **kwargs):
        now[0] += timeout_ms / 1000
        raise ConnectError(Code.DEADLINE_EXCEEDED, "temporary read failure")

    stub.get_sandbox_snapshot.side_effect = unavailable
    with pytest.raises(SandboxSnapshotTimeoutError) as caught:
        saved.restore(timeout=0.25)
    assert caught.value.snapshot_id == "snapshot-1"
    assert "capture continues" in str(caught.value)
    assert now[0] == 0.25
    assert stub.get_sandbox_snapshot.call_args.kwargs["timeout_ms"] == 250
    stub.start_sandbox_snapshot.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        ConnectError(Code.DEADLINE_EXCEEDED, "submission deadline"),
        ConnectError(Code.UNAVAILABLE, "lost acknowledgment"),
        TimeoutError("transport timeout"),
    ],
)
def test_submission_retries_same_request_after_transient_failure(
    snapshot_client, monkeypatch, failure
):
    client, stub = snapshot_client
    monkeypatch.setattr(_sandbox.time, "sleep", lambda _: None)
    stub.start_sandbox_snapshot.side_effect = [
        failure,
        stub.start_sandbox_snapshot.return_value,
    ]
    assert client.get("sandbox-1").snapshot().status == "completed"
    calls = stub.start_sandbox_snapshot.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0] is calls[1].args[0]
    assert calls[0].args[0].idempotency_key
    assert all(call.kwargs["timeout_ms"] == 10_000 for call in calls)


def test_restore_readiness_uses_budget_remaining_after_capture(
    snapshot_client, monkeypatch
):
    client, stub = snapshot_client
    pending = pb.GetSandboxSnapshotResponse(
        snapshot=stub.start_sandbox_snapshot.return_value.snapshot
    )
    stub.get_sandbox_snapshot.return_value = pending
    saved = SandboxSnapshot.from_id("snapshot-1", client=client)
    now = [0.0]
    monkeypatch.setattr(_sandbox.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        _sandbox.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay)
    )

    def complete_capture(request, **kwargs):
        now[0] += 0.1
        return pb.GetSandboxSnapshotResponse(
            snapshot=pb.SandboxSnapshot(
                id="snapshot-1",
                sandbox_id="sandbox-1",
                status=pb.SandboxSnapshotStatus.COMPLETED,
            )
        )

    def create(request, **kwargs):
        assert kwargs["timeout_ms"] == 400
        now[0] += 0.1
        return sandbox_pb.CreateSandboxResponse(
            sandbox=sandbox_pb.SandboxInfo(
                id="restored-1",
                status=sandbox_pb.SandboxStatus.PENDING,
            )
        )

    def readiness_timeout(request, *, timeout_ms, **kwargs):
        assert request.id == "restored-1"
        assert timeout_ms == 300
        now[0] += timeout_ms / 1000
        raise ConnectError(
            Code.DEADLINE_EXCEEDED, "readiness request deadline exceeded"
        )

    stub.get_sandbox_snapshot.side_effect = complete_capture
    sandbox_stub = MagicMock()
    sandbox_stub.create_sandbox.side_effect = create
    sandbox_stub.get_sandbox.side_effect = readiness_timeout
    client._stub = sandbox_stub
    with pytest.raises(
        SandboxCreationError, match="restored-1.*readiness check timed out"
    ):
        saved.restore(timeout=0.5)
    assert now[0] == 0.5
    assert sandbox_stub.create_sandbox.call_count == 1
    stub.start_sandbox_snapshot.assert_not_called()


def test_snapshot_recovers_transient_status_read(snapshot_client, monkeypatch):
    client, stub = snapshot_client
    stub.get_sandbox_snapshot.side_effect = [
        ConnectError(Code.UNAVAILABLE, "rollout"),
        stub.get_sandbox_snapshot.return_value,
    ]
    monkeypatch.setattr(_sandbox.time, "sleep", lambda _: None)
    assert client.get("sandbox-1").snapshot().status == "completed"
    assert stub.get_sandbox_snapshot.call_count == 2
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
    client.get("sandbox-1").snapshot()
    assert len(observed) == 2
    assert observed[0] is observed[1]
    assert observed[0].idempotency_key
