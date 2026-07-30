import asyncio
import base64
import contextvars
from datetime import datetime, timezone

import pyarrow as pa
import pytest

from chalkcompute._function import (
    FunctionError,
    FunctionVersionInfo,
    RemoteFunction,
    _run_in_worker_thread,
)
from chalkcompute._remote_call_client import RemoteCallPollResult, ServiceAuthContext

REQUEST_TIMESTAMP_HEADER = "x-chalk-request-timestamp"


def _result_ipc(values: list[object]) -> bytes:
    batch = pa.record_batch([pa.array(values)], names=["result"])
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, batch.schema) as writer:
        writer.write_batch(batch)
    return sink.getvalue().to_pybytes()


def _read_input(payload: bytes) -> dict[str, list[object]]:
    reader = pa.ipc.open_stream(payload)
    batches = list(reader)
    assert len(batches) == 1
    return batches[0].to_pydict()


class FakeConnectClient:
    _access_token = "access-token"
    _env_id = "env-id"

    def _ensure_auth(self) -> None:
        pass

    def grpc_engine_target(self) -> tuple[str, bool]:
        return "engine.chalk.test:443", True


class ServiceAuthOnlyConnectClient:
    def _ensure_auth(self) -> None:
        raise AssertionError("client credentials auth should not be used")

    def grpc_engine_target(self) -> tuple[str, bool]:
        raise AssertionError("OAuth grpc engine discovery should not be used")


class ServiceAuthDiscoveredEngineConnectClient:
    def grpc_engine_target(self) -> tuple[str, bool]:
        return "discovered-engine.chalk.test:443", True


class RotatingIdentityConnectClient(FakeConnectClient):
    def _uses_web_identity_token(self) -> bool:
        return True


class RpcConnectClient(FakeConnectClient):
    def __init__(self) -> None:
        self.rpc_calls = []

    def rpc(self, *args, **kwargs):
        self.rpc_calls.append((args, kwargs))
        return _RpcResponse()


class OldStyleRpcConnectClient(FakeConnectClient):
    def rpc(self, service, method, body, timeout=30):
        return _RpcResponse()


class _RpcResponse:
    status_code = 200
    text = ""

    def json(self):
        return {
            "remoteCallResponse": {
                "featherStream": base64.b64encode(b"response-ipc").decode("ascii")
            }
        }


class FakeRemoteCallClient:
    instances: list["FakeRemoteCallClient"] = []
    direct_response_chunks: list[bytes] = []
    poll_results: list[RemoteCallPollResult] = []
    call_ipc_errors: list[BaseException] = []
    enqueue_errors: list[BaseException] = []
    poll_errors: list[BaseException] = []
    purge_errors: list[BaseException] = []

    def __init__(
        self, endpoint: str, *, metadata, use_tls: bool | None, **kwargs
    ) -> None:
        self.endpoint = endpoint
        self.metadata = list(metadata)
        self.use_tls = use_tls
        self.kwargs = kwargs
        self.calls: list[tuple[str, bytes, list[tuple[str, str]]]] = []
        self.enqueues: list[tuple[str, bytes, list[tuple[str, str]]]] = []
        self.purges: list[str] = []
        self.closed = False
        FakeRemoteCallClient.instances.append(self)

    def close(self) -> None:
        self.closed = True

    def call_ipc(
        self,
        name: str,
        input_ipc: bytes,
        metadata=(),
    ) -> list[bytes]:
        if self.call_ipc_errors:
            raise self.call_ipc_errors.pop(0)
        self.calls.append((name, input_ipc, list(metadata)))
        return list(self.direct_response_chunks)

    def enqueue(
        self,
        name: str,
        input_ipc: bytes,
        metadata=(),
    ) -> str:
        if self.enqueue_errors:
            raise self.enqueue_errors.pop(0)
        self.enqueues.append((name, input_ipc, list(metadata)))
        return "call-1"

    def poll_next(
        self,
        call_id: str,
        cursor: str = "",
        poll_count: int = 0,
    ) -> RemoteCallPollResult:
        assert call_id == "call-1"
        if self.poll_errors:
            raise self.poll_errors.pop(0)
        return self.poll_results.pop(0)

    def purge_function(self, function_name: str) -> int:
        if self.purge_errors:
            raise self.purge_errors.pop(0)
        self.purges.append(function_name)
        return 7


@pytest.fixture(autouse=True)
def reset_fake_client(monkeypatch):
    import chalkcompute._remote_call_client as remote_call_client

    FakeRemoteCallClient.instances = []
    FakeRemoteCallClient.direct_response_chunks = []
    FakeRemoteCallClient.poll_results = []
    FakeRemoteCallClient.call_ipc_errors = []
    FakeRemoteCallClient.enqueue_errors = []
    FakeRemoteCallClient.poll_errors = []
    FakeRemoteCallClient.purge_errors = []
    monkeypatch.setattr(remote_call_client, "RemoteCallClient", FakeRemoteCallClient)
    monkeypatch.setattr(
        "chalkcompute._function.service_auth_context_from_env", lambda: None
    )


def _remote_function() -> RemoteFunction:
    def add(x: int) -> int:
        return x + 1

    fn = RemoteFunction(add, name="catalog-add")
    fn._web_url = "http://127.0.0.1"
    fn._port = 7777
    fn._client = FakeConnectClient()
    fn._version_info = FunctionVersionInfo(
        id="version-1",
        function_name="catalog-add",
        version=1,
        scaling_group_name="sg",
    )
    return fn


def _async_remote_function() -> RemoteFunction:
    async def add(x: int) -> int:
        return x + 1

    fn = RemoteFunction(add, name="catalog-async-add")
    fn._web_url = "http://127.0.0.1"
    fn._port = 7777
    fn._client = FakeConnectClient()
    fn._version_info = FunctionVersionInfo(
        id="version-1",
        function_name="catalog-async-add",
        version=1,
        scaling_group_name="sg",
    )
    return fn


def test_calling_runs_local_function_in_process_without_wire_call() -> None:
    # If __call__ went over the wire this fake would hand back 999; the local
    # body returns x + 1, and no RemoteCallClient should be constructed.
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([999])]
    FakeRemoteCallClient.instances.clear()

    fn = _remote_function()

    assert fn(41) == 42
    assert FakeRemoteCallClient.instances == []


def test_calling_handle_without_local_func_raises() -> None:
    fn = RemoteFunction._make_empty()
    fn._func_name = "add"

    with pytest.raises(FunctionError, match=r"\.remote\(\)"):
        fn(1, 2)


def test_direct_call_uses_native_client_and_catalog_function_name() -> None:
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]

    fn = _remote_function()

    assert fn.remote(41) == 42

    client = FakeRemoteCallClient.instances[0]
    assert client.endpoint == "127.0.0.1:7777"
    assert client.use_tls is False
    assert ("authorization", "Bearer access-token") in client.metadata
    assert ("x-chalk-env-id", "env-id") in client.metadata
    assert ("x-chalk-deployment-type", "function-queue") not in client.metadata
    assert client.calls[0][0] == "catalog-add"
    assert _read_input(client.calls[0][1]) == {"x": [41]}
    assert client.calls[0][2] == []


def test_sync_direct_call_can_be_awaited_in_async_context() -> None:
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]

    fn = _remote_function()

    async def call() -> int:
        return await fn.remote(41)

    assert asyncio.run(call()) == 42


def test_worker_thread_preserves_contextvars_for_async_remote_calls() -> None:
    request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id")

    async def call() -> str:
        request_id.set("trace-context")
        return await _run_in_worker_thread(lambda: request_id.get())

    assert asyncio.run(call()) == "trace-context"


def test_async_direct_call_can_be_used_synchronously() -> None:
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]

    fn = _async_remote_function()

    assert fn.remote(41) == 42


def test_direct_call_uses_service_auth_from_environment(monkeypatch) -> None:
    monkeypatch.setattr(
        "chalkcompute._function.service_auth_context_from_env",
        lambda: ServiceAuthContext(
            auth_endpoint="https://api.chalk.test",
            exchange_token="exchange-token",
            environment_id="env-service-auth",
            requested_permissions=(2, 3, 11),
            grpc_engine=None,
        ),
    )
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]

    fn = _remote_function()
    fn._client = ServiceAuthOnlyConnectClient()

    assert fn.remote(41) == 42

    client = FakeRemoteCallClient.instances[0]
    assert ("authorization", "Bearer access-token") not in client.metadata
    assert ("x-chalk-env-id", "env-service-auth") in client.metadata
    assert client.kwargs["auth_endpoint"] == "https://api.chalk.test"
    assert client.kwargs["exchange_token"] == "exchange-token"
    assert client.kwargs["requested_permissions"] == (2, 3, 11)


def test_direct_call_injects_fresh_trace_metadata_on_cached_client(monkeypatch) -> None:
    traceparents = [
        "00-11111111111111111111111111111111-1111111111111111-01",
        "00-22222222222222222222222222222222-2222222222222222-01",
    ]

    def next_trace_metadata() -> list[tuple[str, str]]:
        return [("traceparent", traceparents.pop(0))]

    monkeypatch.setattr(
        "chalkcompute._function.current_trace_metadata",
        next_trace_metadata,
    )
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]

    fn = _remote_function()

    assert fn.remote(41) == 42
    assert fn.remote(41) == 42

    assert len(FakeRemoteCallClient.instances) == 1
    assert (
        "traceparent",
        "00-11111111111111111111111111111111-1111111111111111-01",
    ) in FakeRemoteCallClient.instances[0].calls[0][2]
    assert (
        "traceparent",
        "00-22222222222222222222222222222222-2222222222222222-01",
    ) in FakeRemoteCallClient.instances[0].calls[1][2]
    assert not any(
        key == "x-chalk-function-name"
        for key, _ in FakeRemoteCallClient.instances[0].calls[0][2]
    )


def test_direct_call_refreshes_and_retries_on_goaway_stale_client() -> None:
    FakeRemoteCallClient.call_ipc_errors = [
        RuntimeError('gRPC status: status: Unavailable, message: "GOAWAY"')
    ]
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]

    fn = _remote_function()

    assert fn.remote(41) == 42

    assert len(FakeRemoteCallClient.instances) == 2
    assert FakeRemoteCallClient.instances[0].closed is True
    assert FakeRemoteCallClient.instances[1].calls[0][0] == "catalog-add"
    assert _read_input(FakeRemoteCallClient.instances[1].calls[0][1]) == {"x": [41]}


def test_direct_call_drops_non_retryable_stale_client_for_next_call() -> None:
    FakeRemoteCallClient.call_ipc_errors = [
        RuntimeError("gRPC status: status: Unavailable, message: connection refused")
    ]
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]

    fn = _remote_function()

    with pytest.raises(RuntimeError, match="connection refused"):
        fn.remote(41)

    assert len(FakeRemoteCallClient.instances) == 1
    assert FakeRemoteCallClient.instances[0].closed is True
    assert fn._remote_call_client is None

    assert fn.remote(41) == 42
    assert len(FakeRemoteCallClient.instances) == 2


async def _no_sleep(_seconds) -> None:
    return None


def _err(exc_type: str, message: str) -> str:
    """Build a failed-call error payload as the queue surfaces it (flattened
    chalk.common.v1.ChalkError -> JSON ``{"exc_type", "message"}``)."""
    import json

    return json.dumps({"exc_type": exc_type, "message": message})


def _poll(status_name, *, chunks=None, error_message=None):
    status = {"pending": 1, "running": 2, "completed": 3, "failed": 4}[status_name]
    return RemoteCallPollResult(
        status=status,
        status_name=status_name,
        chunks=chunks or [],
        cursor="",
        error_message=error_message,
        poll_count=0,
    )


def test_deferred_get_retries_transient_failure_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr("chalkcompute._function.time.sleep", lambda _s: None)
    FakeRemoteCallClient.poll_results = [
        _poll("failed", error_message=_err("ProxyError", "CallFunction RPC failed")),
        _poll("completed", chunks=[_result_ipc([99])]),
    ]

    fn = _remote_function()
    handle = fn.defer(98)

    assert handle.get(timeout=60) == 99
    # One initial enqueue + one resubmit after the transient failure.
    client = FakeRemoteCallClient.instances[0]
    assert len(client.enqueues) == 2
    assert all(_read_input(e[1]) == {"x": [98]} for e in client.enqueues)


def test_deferred_get_does_not_retry_genuine_user_error(monkeypatch) -> None:
    monkeypatch.setattr("chalkcompute._function.time.sleep", lambda _s: None)
    # The in-pod runtime wraps a user handler exception as RuntimeError; its
    # message mentions the original error but the exc_type is not infra.
    FakeRemoteCallClient.poll_results = [
        _poll(
            "failed",
            error_message=_err(
                "RuntimeError",
                "Exception raised during handler execution: ValueError: bad input",
            ),
        ),
    ]

    fn = _remote_function()
    handle = fn.defer(98)

    with pytest.raises(FunctionError, match="bad input"):
        handle.get(timeout=60)
    # Non-transient error: enqueued once, never resubmitted.
    assert len(FakeRemoteCallClient.instances[0].enqueues) == 1


def test_deferred_get_does_not_retry_unstructured_error(monkeypatch) -> None:
    monkeypatch.setattr("chalkcompute._function.time.sleep", lambda _s: None)
    # A plain (non-JSON) error string can't be positively classified as infra,
    # so it is conservatively treated as non-retryable — even though it mentions
    # "ProxyError" in free text.
    FakeRemoteCallClient.poll_results = [
        _poll("failed", error_message="ProxyError: CallFunction RPC failed"),
    ]

    fn = _remote_function()
    handle = fn.defer(98)

    with pytest.raises(FunctionError):
        handle.get(timeout=60)
    assert len(FakeRemoteCallClient.instances[0].enqueues) == 1


def test_deferred_get_stops_retrying_after_max_attempts(monkeypatch) -> None:
    monkeypatch.setattr("chalkcompute._function.time.sleep", lambda _s: None)

    def transient():
        return _poll("failed", error_message=_err("ProxyError", "connection reset"))

    FakeRemoteCallClient.poll_results = [transient(), transient(), transient()]

    fn = _remote_function()
    handle = fn.defer(98)  # default _max_attempts == 3

    with pytest.raises(FunctionError, match="connection reset"):
        handle.get(timeout=60)
    # 3 attempts total -> initial enqueue + 2 resubmissions.
    assert len(FakeRemoteCallClient.instances[0].enqueues) == 3


def test_sync_stream_retries_when_failure_precedes_any_output(monkeypatch) -> None:
    monkeypatch.setattr("chalkcompute._function.time.sleep", lambda _s: None)
    FakeRemoteCallClient.poll_results = [
        _poll("failed", error_message=_err("ProxyError", "CallFunction RPC failed")),
        _poll("running", chunks=[_result_ipc([0])]),
        _poll("completed", chunks=[_result_ipc([1])]),
    ]

    fn = _remote_function()
    handle = fn.defer(3)

    batches = [b.column("result").to_pylist() for b in handle.stream()]
    assert batches == [[0], [1]]
    assert len(FakeRemoteCallClient.instances[0].enqueues) == 2


def test_sync_stream_does_not_retry_after_yielding(monkeypatch) -> None:
    monkeypatch.setattr("chalkcompute._function.time.sleep", lambda _s: None)
    FakeRemoteCallClient.poll_results = [
        _poll("running", chunks=[_result_ipc([0])]),
        _poll("failed", error_message=_err("ProxyError", "CallFunction RPC failed")),
    ]

    fn = _remote_function()
    handle = fn.defer(3)

    collected = []
    with pytest.raises(FunctionError, match="CallFunction RPC failed"):
        for batch in handle.stream():
            collected.append(batch.column("result").to_pylist())
    # The first batch reached the caller, so the stream is not restarted.
    assert collected == [[0]]
    assert len(FakeRemoteCallClient.instances[0].enqueues) == 1


def test_async_stream_retries_when_failure_precedes_any_output(monkeypatch) -> None:
    monkeypatch.setattr("chalkcompute._function.asyncio.sleep", _no_sleep)
    FakeRemoteCallClient.poll_results = [
        _poll("failed", error_message=_err("ProxyError", "CallFunction RPC failed")),
        _poll("completed", chunks=[_result_ipc([0])]),
    ]

    fn = _async_remote_function()
    handle = fn.defer(3)

    async def _collect() -> list[list[int]]:
        return [b.column("result").to_pylist() async for b in handle.stream()]

    assert asyncio.run(_collect()) == [[0]]
    assert len(FakeRemoteCallClient.instances[0].enqueues) == 2


def test_defer_get_and_purge_use_queue_client() -> None:
    FakeRemoteCallClient.poll_results = [
        RemoteCallPollResult(
            status=1,
            status_name="pending",
            chunks=[],
            cursor="",
            error_message=None,
            poll_count=1,
        ),
        RemoteCallPollResult(
            status=3,
            status_name="completed",
            chunks=[_result_ipc([99])],
            cursor="cursor-1",
            error_message=None,
            poll_count=0,
        ),
    ]

    fn = _remote_function()
    handle = fn.defer(98)

    assert handle.call_id == "call-1"
    assert handle.get() == 99
    assert fn.purge() == 7

    client = FakeRemoteCallClient.instances[0]
    assert client.endpoint == "engine.chalk.test:443"
    assert client.use_tls is True
    assert ("x-chalk-deployment-type", "function-queue") in client.metadata
    assert client.enqueues[0][0] == "catalog-add"
    assert _read_input(client.enqueues[0][1]) == {"x": [98]}
    assert client.enqueues[0][2] == []
    assert client.purges == ["catalog-add"]


def test_poll_refreshes_stale_queue_client_and_retries() -> None:
    FakeRemoteCallClient.poll_results = [
        RemoteCallPollResult(
            status=4,
            status_name="failed",
            chunks=[],
            cursor="",
            error_message='gRPC status: status: Unavailable, message: "GOAWAY"',
            poll_count=0,
        ),
        RemoteCallPollResult(
            status=3,
            status_name="completed",
            chunks=[_result_ipc([99])],
            cursor="",
            error_message=None,
            poll_count=0,
        ),
    ]

    fn = _remote_function()
    handle = fn.defer(98)

    assert handle.get() == 99

    assert len(FakeRemoteCallClient.instances) == 2
    assert FakeRemoteCallClient.instances[0].closed is True
    assert FakeRemoteCallClient.instances[1].endpoint == "engine.chalk.test:443"


def test_deferred_get_can_be_awaited_in_async_context() -> None:
    FakeRemoteCallClient.poll_results = [
        RemoteCallPollResult(
            status=3,
            status_name="completed",
            chunks=[_result_ipc([99])],
            cursor="",
            error_message=None,
            poll_count=0,
        ),
    ]

    fn = _remote_function()
    handle = fn.defer(98)

    async def get() -> int:
        return await handle.get()

    assert asyncio.run(get()) == 99


def test_async_deferred_get_can_be_used_synchronously() -> None:
    FakeRemoteCallClient.poll_results = [
        RemoteCallPollResult(
            status=3,
            status_name="completed",
            chunks=[_result_ipc([99])],
            cursor="",
            error_message=None,
            poll_count=0,
        ),
    ]

    fn = _async_remote_function()
    handle = fn.defer(98)

    assert handle.get() == 99


def test_defer_injects_trace_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        "chalkcompute._function.current_trace_metadata",
        lambda: [
            (
                "traceparent",
                "00-33333333333333333333333333333333-3333333333333333-01",
            )
        ],
    )

    fn = _remote_function()
    handle = fn.defer(98)

    assert handle.call_id == "call-1"
    client = FakeRemoteCallClient.instances[0]
    assert (
        "traceparent",
        "00-33333333333333333333333333333333-3333333333333333-01",
    ) in client.enqueues[0][2]
    assert not any(key == "x-chalk-function-name" for key, _ in client.enqueues[0][2])
    assert client.enqueues[0][0] == "catalog-add"


def test_with_knowledge_cutoff_injects_direct_call_header() -> None:
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]

    fn = _remote_function()
    cutoff = datetime(2026, 5, 1, 12, 30, 45, tzinfo=timezone.utc)

    assert fn.with_knowledge_cutoff(cutoff).remote(41) == 42
    assert (REQUEST_TIMESTAMP_HEADER, "2026-05-01T12:30:45+00:00") in (
        FakeRemoteCallClient.instances[0].calls[0][2]
    )


def test_with_knowledge_cutoff_injects_defer_header_without_mutating_original() -> None:
    FakeRemoteCallClient.poll_results = [
        RemoteCallPollResult(
            status=3,
            status_name="completed",
            chunks=[_result_ipc([99])],
            cursor="",
            error_message=None,
            poll_count=0,
        ),
    ]

    fn = _remote_function()
    cutoff_fn = fn.with_knowledge_cutoff("2026-05-01T12:30:45Z")

    handle = cutoff_fn.defer(98)

    assert handle.get() == 99
    client = FakeRemoteCallClient.instances[0]
    assert (REQUEST_TIMESTAMP_HEADER, "2026-05-01T12:30:45Z") in client.enqueues[0][2]
    assert fn._knowledge_cutoff is None


def test_api_rpc_call_propagates_remote_trace_headers(monkeypatch) -> None:
    monkeypatch.setattr(
        "chalkcompute._function.current_trace_metadata",
        lambda: [("traceparent", "trace-id")],
    )

    fn = _remote_function()
    client = RpcConnectClient()
    fn._client = client

    assert fn._call_via_api_rpc(b"request-ipc") == b"response-ipc"

    args, kwargs = client.rpc_calls[0]
    assert args[2]["remoteCallRequest"]["name"] == "catalog-add"
    assert kwargs["extra_headers"] == {
        "traceparent": "trace-id",
    }


def test_api_rpc_call_propagates_knowledge_cutoff_header() -> None:
    fn = _remote_function().with_knowledge_cutoff("2026-05-01T12:30:45Z")
    client = RpcConnectClient()
    fn._client = client

    assert fn._call_via_api_rpc(b"request-ipc") == b"response-ipc"

    _, kwargs = client.rpc_calls[0]
    assert kwargs["extra_headers"] == {
        REQUEST_TIMESTAMP_HEADER: "2026-05-01T12:30:45Z",
    }


def test_api_rpc_call_does_not_require_extra_headers_by_default() -> None:
    fn = _remote_function()
    fn._client = OldStyleRpcConnectClient()

    assert fn._call_via_api_rpc(b"request-ipc") == b"response-ipc"


def test_direct_call_rebuilds_client_after_identity_token_rotation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "chalkcompute._function.service_auth_context_from_env",
        lambda: ServiceAuthContext(
            auth_endpoint="https://api.chalk.test",
            exchange_token="must-not-be-used",
            environment_id="service-env",
            requested_permissions=(2, 3, 11),
            grpc_engine=("service-engine:443", True),
        ),
    )
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]
    fn = _remote_function()
    connect = RotatingIdentityConnectClient()
    fn._client = connect

    assert fn.remote(41) == 42
    first = FakeRemoteCallClient.instances[0]
    connect._access_token = "rotated-token"
    connect._env_id = "rotated-env"
    assert fn.remote(42) == 42

    assert first.closed is True
    assert len(FakeRemoteCallClient.instances) == 2
    second = FakeRemoteCallClient.instances[1]
    assert ("authorization", "Bearer rotated-token") in second.metadata
    assert ("x-chalk-env-id", "rotated-env") in second.metadata
    assert second.kwargs["exchange_token"] is None


def test_queue_call_uses_service_auth_engine_from_environment(monkeypatch) -> None:
    monkeypatch.setattr(
        "chalkcompute._function.service_auth_context_from_env",
        lambda: ServiceAuthContext(
            auth_endpoint="https://api.chalk.test",
            exchange_token="exchange-token",
            environment_id="env-service-auth",
            requested_permissions=(2, 3, 11),
            grpc_engine=("engine.chalk.test:443", True),
        ),
    )
    FakeRemoteCallClient.poll_results = [
        RemoteCallPollResult(
            status=3,
            status_name="completed",
            chunks=[_result_ipc([99])],
            cursor="",
            error_message=None,
            poll_count=0,
        ),
    ]

    fn = _remote_function()
    fn._client = ServiceAuthOnlyConnectClient()
    handle = fn.defer(98)

    assert handle.call_id == "call-1"
    assert handle.get() == 99

    client = FakeRemoteCallClient.instances[0]
    assert client.endpoint == "engine.chalk.test:443"
    assert client.use_tls is True
    assert ("x-chalk-env-id", "env-service-auth") in client.metadata
    assert ("x-chalk-deployment-type", "function-queue") in client.metadata
    assert client.kwargs["auth_endpoint"] == "https://api.chalk.test"
    assert client.kwargs["exchange_token"] == "exchange-token"


def test_queue_call_uses_service_auth_engine_from_token_response(monkeypatch) -> None:
    monkeypatch.setattr(
        "chalkcompute._function.service_auth_context_from_env",
        lambda: ServiceAuthContext(
            auth_endpoint="https://api.chalk.test",
            exchange_token="exchange-token",
            environment_id="env-service-auth",
            requested_permissions=(2, 3, 11),
            grpc_engine=None,
        ),
    )
    FakeRemoteCallClient.poll_results = [
        RemoteCallPollResult(
            status=3,
            status_name="completed",
            chunks=[_result_ipc([99])],
            cursor="",
            error_message=None,
            poll_count=0,
        ),
    ]

    fn = _remote_function()
    fn._client = ServiceAuthDiscoveredEngineConnectClient()
    handle = fn.defer(98)

    assert handle.call_id == "call-1"
    assert handle.get() == 99

    client = FakeRemoteCallClient.instances[0]
    assert client.endpoint == "discovered-engine.chalk.test:443"
    assert client.use_tls is True
    assert ("x-chalk-env-id", "env-service-auth") in client.metadata
    assert ("x-chalk-deployment-type", "function-queue") in client.metadata
    assert client.kwargs["auth_endpoint"] == "https://api.chalk.test"
    assert client.kwargs["exchange_token"] == "exchange-token"


def test_delete_drops_cached_call_clients() -> None:
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]

    fn = _remote_function()
    fn._client = RpcConnectClient()

    assert fn.remote(41) == 42
    handle = fn.defer(98)
    assert handle.call_id == "call-1"

    direct_client = FakeRemoteCallClient.instances[0]
    queue_client = FakeRemoteCallClient.instances[1]

    fn.delete()

    assert direct_client.closed is True
    assert queue_client.closed is True
    assert fn._remote_call_client is None
    assert fn._queue_call_client is None
    assert fn._version_info is None
