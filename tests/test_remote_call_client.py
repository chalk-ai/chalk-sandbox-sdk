import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from datetime import datetime, timezone

import pyarrow as pa
import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf import Oneof

import chalkcompute._function as function_mod
from chalkcompute._async_utils import _run_in_worker_thread
from chalkcompute._function import (
    FunctionError,
    FunctionVersionInfo,
    RemoteFunction,
)
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as catalog_pb
from chalkcompute._gen.chalk.runtime.v1 import remote_python_call_pb as remote_call_pb
from chalkcompute._remote_call_client import (
    RemoteCallPollResult,
    RemoteCallClientUnavailable,
    RemoteCallNotFoundError,
    RemoteCallStatusError,
    RemoteCallTransportError,
    RemoteCallUnavailableError,
    ServiceAuthContext,
)
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from tests._fakes import FakeMethod, StubbedConnectClient

REQUEST_TIMESTAMP_HEADER = "x-chalk-request-timestamp"


@pytest.mark.parametrize("queue", [False, True])
@pytest.mark.parametrize("late_error", [False, True])
def test_retired_transport_keeps_borrowers_alive_and_preserves_replacement(
    queue, late_error
) -> None:
    fn = _remote_function()
    first = fn._ensure_remote_call_client(queue=queue)
    entered = threading.Event()
    release = threading.Event()

    def pending(client):
        assert client is first
        entered.set()
        assert release.wait(3)
        assert not client.closed
        if late_error:
            raise RuntimeError("gRPC status: Unavailable: GOAWAY")
        return 42

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn._using_remote_call_client, queue=queue, call=pending)
        try:
            assert entered.wait(3)
            fn._drop_remote_call_client(queue=queue, expected=first)
            assert not first.closed
            replacement = fn._ensure_remote_call_client(queue=queue)
            assert replacement is not first
        finally:
            release.set()
        if late_error:
            with pytest.raises(RuntimeError, match="GOAWAY"):
                future.result(timeout=3)
        else:
            assert future.result(timeout=3) == 42
    assert first.closed
    assert fn._ensure_remote_call_client(queue=queue) is replacement
    assert not replacement.closed


def test_python_deadline_does_not_release_native_transport_lease() -> None:
    fn = _remote_function()
    fn._version_info = None
    first = fn._ensure_remote_call_client()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def pending(client):
        entered.set()
        assert release.wait(3)
        assert not client.closed
        return 42

    def invoke():
        try:
            return fn._using_remote_call_client(queue=False, call=pending)
        finally:
            finished.set()

    try:
        with pytest.raises(TimeoutError):
            fn._call_with_deadline(
                invoke, time.monotonic() + 0.1, configured_timeout=0.1
            )
        assert entered.is_set()
        fn._drop_remote_call_clients()
        assert not first.closed
        assert fn._ensure_remote_call_client() is not first
    finally:
        release.set()
        assert finished.wait(3)
    # Worker signals finished after the lease's finally has run.
    assert first.closed


@pytest.mark.parametrize("queue", [False, True])
def test_concurrent_transport_creation_is_single_flight(monkeypatch, queue) -> None:
    fn = _remote_function()
    original = fn._create_remote_call_client
    entered = threading.Event()
    release = threading.Event()
    creations = []

    def create(**kwargs):
        client = original(**kwargs)
        creations.append(client)
        entered.set()
        assert release.wait(3)
        return client

    monkeypatch.setattr(fn, "_create_remote_call_client", create)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(fn._ensure_remote_call_client, queue=queue) for _ in range(8)
        ]
        try:
            assert entered.wait(3)
        finally:
            release.set()
        clients = [future.result(timeout=3) for future in futures]
    assert len(creations) == 1
    assert all(client is creations[0] for client in clients)


def test_cache_reset_discards_connection_built_before_reset(monkeypatch) -> None:
    fn = _remote_function()
    original = fn._create_remote_call_client
    entered = threading.Event()
    release = threading.Event()
    creations = []

    def create(**kwargs):
        client = original(**kwargs)
        creations.append(client)
        if len(creations) == 1:
            entered.set()
            assert release.wait(3)
        return client

    monkeypatch.setattr(fn, "_create_remote_call_client", create)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn._ensure_remote_call_client)
        try:
            assert entered.wait(3)
            fn._drop_remote_call_clients()
        finally:
            release.set()
        client = future.result(timeout=3)
    assert len(creations) == 2
    assert creations[0].closed
    assert client is creations[1]
    assert fn._direct_transport.client is client


@pytest.mark.parametrize("queue", [False, True])
def test_borrow_uses_replacement_before_dispatch(queue) -> None:
    fn = _remote_function()
    first = fn._ensure_remote_call_client(queue=queue)
    fn._drop_remote_call_client(queue=queue, expected=first)
    dispatched = []

    def call(client):
        assert client is not first
        assert not client.closed
        dispatched.append(client)
        return 42

    assert fn._using_remote_call_client(queue=queue, call=call) == 42
    assert first.closed
    assert dispatched == [fn._ensure_remote_call_client(queue=queue)]


def test_failed_connection_creation_wakes_other_callers(monkeypatch) -> None:
    fn = _remote_function()
    original = fn._create_remote_call_client
    entered = threading.Event()
    release = threading.Event()
    attempts = []

    def create(**kwargs):
        attempts.append(None)
        if len(attempts) == 1:
            entered.set()
            assert release.wait(3)
            raise RuntimeError("connect failed")
        return original(**kwargs)

    monkeypatch.setattr(fn, "_create_remote_call_client", create)
    with ThreadPoolExecutor(max_workers=8) as pool:
        first = pool.submit(fn._ensure_remote_call_client)
        try:
            assert entered.wait(3)
            others = [pool.submit(fn._ensure_remote_call_client) for _ in range(7)]
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="connect failed"):
            first.result(timeout=3)
        clients = [future.result(timeout=3) for future in others]
    assert len(attempts) == 2
    assert all(client is clients[0] for client in clients)


def test_python_grpc_fallback_keeps_its_transport_until_rpc_finishes(monkeypatch):
    fn = _remote_function()
    fn._call_timeout = None

    def unavailable(**kwargs):
        raise RemoteCallClientUnavailable("native client unavailable")

    monkeypatch.setattr(fn, "_create_remote_call_client", unavailable)

    class Stub:
        def __init__(self, retire=False):
            self.retire = retire
            self.closed = False

        def call_function(self, requests, **kwargs):
            request = next(requests)
            if self.retire:
                fn._drop_remote_call_client(queue=False, expected=self)
                fn._ensure_remote_call_client()
                assert not self.closed
            value = _read_input(request.feather_stream)["x"][0]
            return [
                remote_call_pb.CallFunctionResponse(
                    feather_stream=_result_ipc([value + 1])
                )
            ]

        def close(self):
            self.closed = True

    first = Stub(retire=True)
    replacement = Stub()
    stubs = iter([first, replacement])
    monkeypatch.setattr(
        "chalkcompute._gen.chalk.runtime.v1.remote_python_call_connect.RemoteCallServiceClientSync",
        lambda *args, **kwargs: next(stubs),
    )
    assert fn.remote(41) == 42
    assert first.closed
    assert fn._direct_transport.client is replacement
    assert not replacement.closed
    assert fn.remote(42) == 43


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


class FakeConnectClient(StubbedConnectClient):
    _access_token = "access-token"
    _env_id = "env-id"

    def _ensure_auth(self) -> None:
        pass

    def grpc_engine_target(self) -> tuple[str, bool]:
        return "engine.chalk.test:443", True

    def dataplane_api_server_target(self) -> tuple[str, bool]:
        return "dataplane-api-server.chalk.test:443", True


class ServiceAuthOnlyConnectClient:
    def _ensure_auth(self) -> None:
        raise AssertionError("client credentials auth should not be used")

    def grpc_engine_target(self) -> tuple[str, bool]:
        raise AssertionError("OAuth grpc engine discovery should not be used")

    def dataplane_api_server_target(self) -> tuple[str, bool]:
        return "dataplane-api-server.chalk.test:443", True


class ServiceAuthDiscoveredEngineConnectClient:
    def grpc_engine_target(self) -> tuple[str, bool]:
        return "discovered-engine.chalk.test:443", True

    def dataplane_api_server_target(self) -> tuple[str, bool]:
        return "dataplane-api-server.chalk.test:443", True


class RotatingIdentityConnectClient(FakeConnectClient):
    def _uses_web_identity_token(self) -> bool:
        return True


class RpcConnectClient(FakeConnectClient):
    def __init__(self) -> None:
        self.rpc_calls = []

    def call(self, method: FakeMethod, request: object, **kwargs: object) -> object:
        self.rpc_calls.append((method.name, request, kwargs))
        if method.name == "delete_external_function":
            return catalog_pb.DeleteExternalFunctionResponse()
        assert method.name == "call_external_function"
        return catalog_pb.CallExternalFunctionResponse(
            body=Oneof(
                "remote_call_response",
                remote_call_pb.CallFunctionResponse(feather_stream=b"response-ipc"),
            )
        )


class FakeRemoteCallClient:
    instances: list["FakeRemoteCallClient"] = []
    direct_response_chunks: list[bytes] = []
    poll_results: list[RemoteCallPollResult] = []
    call_ipc_errors: list[BaseException] = []
    enqueue_errors: list[BaseException] = []
    poll_errors: list[BaseException] = []
    purge_errors: list[BaseException] = []
    # Simulates the native client hitting a crash-looping replica: call_ipc
    # has no timeout param of its own, so it just never returns.
    hang_forever: bool = False

    def __init__(
        self, endpoint: str, *, metadata, use_tls: bool | None, **kwargs
    ) -> None:
        self.endpoint = endpoint
        self.metadata = list(metadata)
        self.use_tls = use_tls
        self.kwargs = kwargs
        self.calls: list[tuple[str, bytes, list[tuple[str, str]]]] = []
        self.enqueues: list[tuple[str, bytes, list[tuple[str, str]]]] = []
        self.call_options: list[dict] = []
        self.enqueue_options: list[dict] = []
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
        **options,
    ) -> list[bytes]:
        self.call_options.append(options)
        if self.hang_forever:
            import threading

            threading.Event().wait()
        if self.call_ipc_errors:
            raise self.call_ipc_errors.pop(0)
        self.calls.append((name, input_ipc, list(metadata)))
        return list(self.direct_response_chunks)

    def enqueue(
        self,
        name: str,
        input_ipc: bytes,
        metadata=(),
        **options,
    ) -> str:
        self.enqueue_options.append(options)
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
    FakeRemoteCallClient.hang_forever = False
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
    fn._function_id = "function-1"
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
    fn._function_id = "function-1"
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


@pytest.mark.parametrize("async_call", [False, True])
def test_remote_execution_error_is_never_retried(async_call: bool) -> None:
    import json
    from chalkcompute import RemoteExecutionError
    from chalkcompute._remote_call_client import RemoteCallStatusError
    from tests.test_remote_execution_error import payload

    wire = json.loads(payload())
    wire["exception"]["message"] = "transport error: GOAWAY connection reset"
    FakeRemoteCallClient.call_ipc_errors = [RemoteCallStatusError(2, json.dumps(wire))]
    fn = _remote_function()

    async def call():
        return await fn.remote(1)

    with pytest.raises(RemoteExecutionError):
        asyncio.run(call()) if async_call else fn.remote(1)
    assert len(FakeRemoteCallClient.instances) == 1
    assert not FakeRemoteCallClient.instances[0].closed


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("async_call", [False, True])
def test_deferred_remote_execution_error_preserves_details(
    stream: bool, async_call: bool
) -> None:
    from chalkcompute import RemoteExecutionError
    from tests.test_remote_execution_error import payload

    FakeRemoteCallClient.poll_results = [
        _poll("failed", error_message=_err("ProxyError", payload())),
    ]
    fn = _remote_function()
    handle = fn.defer(1)

    handle._is_async = async_call

    async def call():
        if stream:
            return [item async for item in handle.stream()]
        return await handle.get()

    with pytest.raises(RemoteExecutionError) as caught:
        if async_call:
            asyncio.run(call())
        elif stream:
            list(handle.stream())
        else:
            handle.get()
    assert caught.value.call_id == "call-1"
    assert len(FakeRemoteCallClient.instances[0].enqueues) == 1


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


def test_direct_call_retires_goaway_transport_without_redispatch() -> None:
    FakeRemoteCallClient.call_ipc_errors = [
        RuntimeError('gRPC status: status: Unavailable, message: "GOAWAY"')
    ]
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]

    fn = _remote_function()

    with pytest.raises(RuntimeError, match="GOAWAY"):
        fn.remote(41)
    assert len(FakeRemoteCallClient.instances) == 1
    assert FakeRemoteCallClient.instances[0].closed is True


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
    assert fn._direct_transport.client is None

    assert fn.remote(41) == 42
    assert len(FakeRemoteCallClient.instances) == 2


def test_direct_call_retires_transport_error_without_redispatch(monkeypatch) -> None:
    monkeypatch.setattr("chalkcompute._function.time.sleep", lambda _s: None)
    FakeRemoteCallClient.call_ipc_errors = [RemoteCallTransportError("disconnected")]
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]

    fn = _remote_function()

    with pytest.raises(RemoteCallTransportError, match="disconnected"):
        fn.remote(41)
    assert len(FakeRemoteCallClient.instances) == 1
    assert FakeRemoteCallClient.instances[0].closed is True


def test_direct_call_does_not_retry_deadline_exceeded() -> None:
    FakeRemoteCallClient.call_ipc_errors = [
        ConnectRpcError(Code.DEADLINE_EXCEEDED, "deadline exceeded")
    ]

    fn = _remote_function()

    with pytest.raises(ConnectRpcError) as exc_info:
        fn.remote(41)

    assert exc_info.value.code == Code.DEADLINE_EXCEEDED
    assert len(FakeRemoteCallClient.instances) == 1


async def _no_sleep(_seconds) -> None:
    return None


def _err(exc_type: str, message: str) -> str:
    """Build a failed-call error payload as the queue surfaces it (flattened
    chalk.common.v1.ChalkError -> JSON ``{"exc_type", "message"}``)."""
    import json

    return json.dumps({"exc_type": exc_type, "message": message})


def _poll(status_name, *, chunks=None, error_message=None):
    status = {"pending": 1, "running": 2, "completed": 3, "failed": 4, "expired": 5}[
        status_name
    ]
    return RemoteCallPollResult(
        status=status,
        status_name=status_name,
        chunks=chunks or [],
        cursor="",
        error_message=error_message,
        poll_count=0,
    )


def test_deferred_get_preserves_call_id_after_terminal_infra_failure() -> None:
    FakeRemoteCallClient.poll_results = [
        _poll("failed", error_message=_err("ProxyError", "CallFunction RPC failed")),
    ]
    handle = _remote_function().defer(98)
    with pytest.raises(FunctionError, match="CallFunction RPC failed"):
        handle.get(timeout=60)
    assert handle.call_id == "call-1"
    assert len(FakeRemoteCallClient.instances[0].enqueues) == 1


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


def test_deferred_get_fails_when_results_have_expired() -> None:
    FakeRemoteCallClient.poll_results = [_poll("expired")]

    handle = _remote_function().defer(98)

    with pytest.raises(FunctionError, match="results have expired"):
        handle.get()
    assert len(FakeRemoteCallClient.instances[0].enqueues) == 1


def test_deferred_get_fails_when_call_is_no_longer_retained() -> None:
    from chalkcompute._remote_call_client import RemoteCallNotFoundError

    FakeRemoteCallClient.poll_errors = [
        RemoteCallNotFoundError("remote call was not found or is no longer retained")
    ]

    handle = _remote_function().defer(98)

    with pytest.raises(FunctionError, match="not found or is no longer retained"):
        handle.get()
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


def test_sync_stream_does_not_resubmit_before_first_output() -> None:
    FakeRemoteCallClient.poll_results = [
        _poll("failed", error_message=_err("ProxyError", "CallFunction RPC failed")),
    ]
    handle = _remote_function().defer(3)
    with pytest.raises(FunctionError, match="CallFunction RPC failed"):
        list(handle.stream())
    assert handle.call_id == "call-1"
    assert len(FakeRemoteCallClient.instances[0].enqueues) == 1


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


def test_sync_stream_fails_when_results_have_expired() -> None:
    FakeRemoteCallClient.poll_results = [_poll("expired")]

    handle = _remote_function().defer(3)

    with pytest.raises(FunctionError, match="results have expired"):
        list(handle.stream())
    assert len(FakeRemoteCallClient.instances[0].enqueues) == 1


def test_async_stream_does_not_resubmit_before_first_output() -> None:
    FakeRemoteCallClient.poll_results = [
        _poll("failed", error_message=_err("ProxyError", "CallFunction RPC failed")),
    ]
    handle = _async_remote_function().defer(3)

    async def collect():
        return [batch async for batch in handle.stream()]

    with pytest.raises(FunctionError, match="CallFunction RPC failed"):
        asyncio.run(collect())
    assert handle.call_id == "call-1"
    assert len(FakeRemoteCallClient.instances[0].enqueues) == 1


def test_async_stream_fails_when_results_have_expired() -> None:
    FakeRemoteCallClient.poll_results = [_poll("expired")]

    handle = _async_remote_function().defer(3)

    async def _collect() -> list[list[int]]:
        return [batch.column("result").to_pylist() async for batch in handle.stream()]

    with pytest.raises(FunctionError, match="results have expired"):
        asyncio.run(_collect())
    assert len(FakeRemoteCallClient.instances[0].enqueues) == 1


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
    assert client.endpoint == "dataplane-api-server.chalk.test:443"
    assert client.use_tls is True
    assert ("x-chalk-deployment-type", "function-queue") not in client.metadata
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
    assert (
        FakeRemoteCallClient.instances[1].endpoint
        == "dataplane-api-server.chalk.test:443"
    )


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

    method, request, kwargs = client.rpc_calls[0]
    assert method == "call_external_function"
    assert isinstance(request, catalog_pb.CallExternalFunctionRequest)
    assert request.body is not None
    assert request.body.value.name == "catalog-add"
    assert kwargs["extra_headers"] == {
        "traceparent": "trace-id",
    }


def test_api_rpc_call_propagates_knowledge_cutoff_header() -> None:
    fn = _remote_function().with_knowledge_cutoff("2026-05-01T12:30:45Z")
    client = RpcConnectClient()
    fn._client = client

    assert fn._call_via_api_rpc(b"request-ipc") == b"response-ipc"

    _method, _request, kwargs = client.rpc_calls[0]
    assert kwargs["extra_headers"] == {
        REQUEST_TIMESTAMP_HEADER: "2026-05-01T12:30:45Z",
    }


def test_api_rpc_call_omits_extra_headers_by_default() -> None:
    fn = _remote_function()
    client = RpcConnectClient()
    fn._client = client

    assert fn._call_via_api_rpc(b"request-ipc") == b"response-ipc"
    assert client.rpc_calls[0][2]["extra_headers"] is None


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
    assert client.endpoint == "dataplane-api-server.chalk.test:443"
    assert client.use_tls is True
    assert ("x-chalk-env-id", "env-service-auth") in client.metadata
    assert ("x-chalk-deployment-type", "function-queue") not in client.metadata
    assert client.kwargs["auth_endpoint"] == "https://api.chalk.test"
    assert client.kwargs["exchange_token"] == "exchange-token"


def test_queue_call_uses_service_auth_without_engine_endpoint(monkeypatch) -> None:
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
    assert client.endpoint == "dataplane-api-server.chalk.test:443"
    assert client.use_tls is True
    assert ("x-chalk-env-id", "env-service-auth") in client.metadata
    assert ("x-chalk-deployment-type", "function-queue") not in client.metadata
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
    assert fn._direct_transport.client is None
    assert fn._queue_transport.client is None
    assert fn._version_info is None


def test_direct_call_times_out_instead_of_hanging_forever() -> None:
    # Simulates CHA-10415: the scaling group is crash-looping, so the native
    # call_ipc (no timeout param of its own) never returns.
    FakeRemoteCallClient.hang_forever = True

    fn = _remote_function()
    fn._call_timeout = 0.2

    started = time.monotonic()
    with pytest.raises(TimeoutError, match=r"timed out after 0\.2s"):
        fn.remote(41)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0


def test_call_timeout_error_includes_scaling_group_status(monkeypatch) -> None:
    FakeRemoteCallClient.hang_forever = True

    fn = _remote_function()
    fn._call_timeout = 0.2

    monkeypatch.setattr(
        function_mod,
        "_get_scaling_group",
        lambda client, name: sg_pb.ScalingGroupResponse(
            name=name,
            status="Unhealthy",
            status_message="1/1 replicas unhealthy: Error (exit code 1)",
        ),
    )

    with pytest.raises(
        TimeoutError, match=r"Unhealthy.*1/1 replicas unhealthy: Error \(exit code 1\)"
    ):
        fn.remote(41)


def test_call_timeout_none_disables_deadline() -> None:
    # Explicit opt-out: call_timeout=None preserves the old unbounded wait.
    FakeRemoteCallClient.hang_forever = True

    fn = _remote_function()
    fn._call_timeout = None

    result: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result.update(value=fn.remote(41)), daemon=True
    )
    thread.start()
    thread.join(timeout=1.0)

    assert thread.is_alive()
    assert result == {}


def test_retry_options_are_scoped_and_preserve_argument_named_retries() -> None:
    from chalkcompute._retry import RetryPolicy

    def handler(retries: int) -> int:
        return retries

    fn = _remote_function()
    fn._call_parameter_names = ("retries",)
    fn._input_field_names = ("retries",)
    fn._bind_call_arguments = function_mod._compile_fixed_argument_binder(
        ("retries",), positional_count=1, defaults=(function_mod._MISSING_ARGUMENT,)
    )
    fn._retries = RetryPolicy.exponential(attempts=4)
    scoped = fn.with_options(retries=0)
    scoped.defer(retries=98)
    fn.defer(retries=99)
    client = FakeRemoteCallClient.instances[0]
    assert client.enqueue_options == [{"max_retries": 0}, {}]
    assert _read_input(client.enqueues[0][1]) == {"retries": [98]}
    assert fn._retries.max_retries == 4
    assert scoped._effective_retry_policy().max_retries == 0
    assert scoped.with_options(retries=None)._effective_retry_policy().max_retries == 4


def test_direct_retry_policy_is_forwarded_once_to_native_client() -> None:
    from chalkcompute._retry import RetryPolicy

    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]
    policy = RetryPolicy.linear(attempts=2, delay=0.25)
    fn = _remote_function().with_options(retries=policy)
    assert fn.remote(41) == 42
    client = FakeRemoteCallClient.instances[0]
    assert client.call_options == [
        {"retry_policy": policy.to_dict(), "timeout": pytest.approx(60, abs=1)}
    ]
    assert len(client.calls) == 1


def test_attached_function_hydrates_retry_defaults_and_preserves_override() -> None:
    fn = _remote_function()
    fn._has_local_func = False
    scoped = fn.with_options(retries=0)
    version = catalog_pb.ExternalFunctionVersion(
        id="version-1",
        function_name="catalog-add",
        version=1,
        config=catalog_pb.FunctionConfig(
            retry_policy=catalog_pb.RetryPolicy(
                max_retries=3,
                initial_backoff_ms=250,
                backoff_multiplier=1,
                max_backoff_ms=250,
                jitter=False,
            )
        ),
    )
    function_mod._apply_function_version(fn, version, is_current=True)
    function_mod._apply_function_version(scoped, version, is_current=True)
    assert fn._effective_retry_policy().max_retries == 3
    assert fn._effective_retry_policy().initial_wait_seconds == 0.25
    assert fn._effective_retry_policy().jitter is False
    assert scoped._effective_retry_policy().max_retries == 0


def test_bound_class_method_supports_scoped_retry_options() -> None:
    from chalkcompute._cls import _BoundRemoteMethod

    fn = _remote_function()
    bound = _BoundRemoteMethod("add", remote_function=fn)
    scoped = bound.with_options(retries=2)
    assert scoped is not fn
    assert scoped._effective_retry_policy().max_retries == 2
    assert fn._effective_retry_policy().max_retries == 0


@pytest.mark.parametrize("retries", [None, 0, 2])
def test_api_retry_call_uses_native_adapter(monkeypatch, retries) -> None:
    from chalkcompute._retry import RetryPolicy

    calls = []

    class ApiNative:
        def __init__(self, endpoint, **kwargs):
            calls.append((endpoint, kwargs))

        def call_external_ipc(self, name, version, payload, **kwargs):
            calls.append((name, version, payload, kwargs))
            return [_result_ipc([42])]

        def close(self):
            calls.append("closed")

    monkeypatch.setenv("CHALK_FUNCTION_CALL_VIA_API", "1")
    # Match the actual route switch rather than depending on process environment.
    monkeypatch.setattr(function_mod, "_should_call_via_api", lambda: True)
    monkeypatch.setattr("chalkcompute._remote_call_client.RemoteCallClient", ApiNative)
    fn = _remote_function().with_options(retries=retries)
    fn._client._api_server = "https://api.chalk.test"
    assert fn.remote(41) == 42
    assert calls[0][0] == "https://api.chalk.test"
    assert calls[0][1]["api_mode"] is True
    assert calls[1][0:2] == ("catalog-add", 1)
    assert (
        calls[1][3]["retry_policy"]
        == RetryPolicy.exponential(attempts=retries or 0).to_dict()
    )
    assert calls[1][3]["timeout"] == pytest.approx(60, abs=1)
    assert calls[-1] == "closed"


@pytest.mark.parametrize("override", [None, 0])
def test_zero_retry_native_call_has_one_owner(override):
    fn = _remote_function().with_options(retries=override)
    FakeRemoteCallClient.call_ipc_errors = [RemoteCallTransportError("response lost")]
    with pytest.raises(RemoteCallTransportError, match="response lost"):
        fn.remote(41)
    assert len(FakeRemoteCallClient.instances) == 1
    assert len(FakeRemoteCallClient.instances[0].call_options) == 1
    assert (
        FakeRemoteCallClient.instances[0].call_options[0]["retry_policy"]["max_retries"]
        == 0
    )


def test_enqueue_transport_error_never_submits_a_second_call():
    FakeRemoteCallClient.enqueue_errors = [RemoteCallTransportError("response lost")]
    with pytest.raises(RemoteCallTransportError, match="response lost"):
        _remote_function().with_options(retries=2).defer(41)
    assert len(FakeRemoteCallClient.instances) == 1
    assert len(FakeRemoteCallClient.instances[0].enqueue_options) == 1


_NO_REPLICA = (
    "upstream connect error or disconnect/reset before headers. "
    "reset reason: remote connection failure"
)


class FakeClock:
    """Stands in for ``time`` inside ``_function`` so backoff runs instantly."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds

    def __getattr__(self, name: str):
        return getattr(time, name)


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(function_mod, "time", fake)
    return fake


@pytest.mark.parametrize(
    "message",
    [
        _NO_REPLICA,
        "no healthy upstream",
        "upstream connect error or disconnect/reset before headers. "
        "retried and the latest reset reason: connection timeout",
    ],
)
def test_direct_call_resends_until_a_replica_accepts(clock, message) -> None:
    from chalkcompute._retry import RetryPolicy

    FakeRemoteCallClient.call_ipc_errors = [
        RemoteCallUnavailableError(message) for _ in range(3)
    ]
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]
    policy = RetryPolicy.linear(attempts=2, delay=0.25)
    fn = _remote_function().with_options(retries=policy)
    fn._call_timeout = 180

    assert fn.remote(41) == 42
    assert clock.sleeps == [0.25, 0.5, 1.0]
    options = [o for c in FakeRemoteCallClient.instances for o in c.call_options]
    assert [o["retry_policy"] for o in options] == [policy.to_dict()] * 4
    assert [o["timeout"] for o in options] == [180, 179.75, 179.25, 178.25]


def test_no_replica_retries_stop_at_call_timeout(clock) -> None:
    FakeRemoteCallClient.call_ipc_errors = [
        RemoteCallUnavailableError(_NO_REPLICA) for _ in range(1000)
    ]
    fn = _remote_function()
    fn._call_timeout = 180
    started = clock.now

    with pytest.raises(RemoteCallUnavailableError, match="remote connection failure"):
        fn.remote(41)

    assert clock.now - started == pytest.approx(180)
    assert max(clock.sleeps) == function_mod._NO_REPLICA_MAX_BACKOFF_SECONDS
    assert 36 < len(clock.sleeps) < 45


def test_no_replica_retries_without_limit_when_call_timeout_is_none(clock) -> None:
    FakeRemoteCallClient.call_ipc_errors = [
        RemoteCallUnavailableError(_NO_REPLICA) for _ in range(2000)
    ]
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]
    fn = _remote_function()
    fn._call_timeout = None
    started = clock.now

    assert fn.remote(41) == 42
    assert clock.now - started > 9000
    options = [o for c in FakeRemoteCallClient.instances for o in c.call_options]
    assert len(options) == 2001
    assert all(o["timeout"] is None for o in options)


def _user_error() -> BaseException:
    import json
    from tests.test_remote_execution_error import payload

    wire = json.loads(payload())
    wire["exception"]["message"] = "no healthy upstream"
    return RemoteCallStatusError(2, json.dumps(wire))


@pytest.mark.parametrize(
    "error,expected",
    [
        (_user_error, "RemoteExecutionError"),
        (lambda: RemoteCallNotFoundError("function not found"), None),
        (lambda: ConnectRpcError(Code.UNAUTHENTICATED, "no healthy upstream"), None),
        (lambda: ConnectRpcError(Code.PERMISSION_DENIED, "denied"), None),
        (lambda: RemoteCallTransportError("connection failure"), None),
        (
            lambda: RemoteCallUnavailableError(
                "upstream connect error or disconnect/reset before headers. "
                "reset reason: connection termination"
            ),
            None,
        ),
        (lambda: RemoteCallUnavailableError("GOAWAY"), None),
    ],
)
def test_errors_after_dispatch_or_not_capacity_fail_fast(
    clock, error, expected
) -> None:
    from chalkcompute import RemoteExecutionError

    instance = error()
    FakeRemoteCallClient.call_ipc_errors = [instance]
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]
    fn = _remote_function()

    expected_type = RemoteExecutionError if expected else type(instance)
    with pytest.raises(expected_type):
        fn.remote(41)
    assert clock.sleeps == []
    assert sum(len(c.call_options) for c in FakeRemoteCallClient.instances) == 1


def test_no_replica_deadline_includes_client_rebuilds(clock, monkeypatch) -> None:
    # A Connect UNAVAILABLE also marks the cached client stale, so each resend
    # rebuilds it; that time must come out of the same call_timeout budget.
    FakeRemoteCallClient.call_ipc_errors = [
        ConnectRpcError(Code.UNAVAILABLE, "no healthy upstream") for _ in range(100)
    ]
    original_init = FakeRemoteCallClient.__init__

    def slow_init(self, *args, **kwargs):
        clock.now += 10
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(FakeRemoteCallClient, "__init__", slow_init)
    fn = _remote_function()
    fn._call_timeout = 30
    started = clock.now

    with pytest.raises(ConnectRpcError, match="no healthy upstream"):
        fn.remote(41)

    timeouts = [c.call_options[0]["timeout"] for c in FakeRemoteCallClient.instances]
    assert timeouts == pytest.approx([20, 9.75, 0])
    assert all(c.closed for c in FakeRemoteCallClient.instances)
    assert clock.now - started == pytest.approx(30.75)


def test_call_timeout_shorter_than_backoff_makes_one_attempt(clock) -> None:
    FakeRemoteCallClient.call_ipc_errors = [
        RemoteCallUnavailableError(_NO_REPLICA) for _ in range(10)
    ]
    fn = _remote_function()
    fn._call_timeout = 0.1

    with pytest.raises(RemoteCallUnavailableError):
        fn.remote(41)
    assert clock.sleeps == [pytest.approx(0.1)]
    assert sum(len(c.call_options) for c in FakeRemoteCallClient.instances) == 1


@pytest.mark.parametrize("call_timeout", [0, -5])
def test_nonpositive_call_timeout_fails_without_dispatch(clock, call_timeout) -> None:
    FakeRemoteCallClient.call_ipc_errors = [RemoteCallUnavailableError(_NO_REPLICA)]
    fn = _remote_function()
    fn._call_timeout = call_timeout

    with pytest.raises(TimeoutError):
        fn.remote(41)
    assert clock.sleeps == []
    assert sum(len(c.call_options) for c in FakeRemoteCallClient.instances) == 0


def test_api_call_resends_until_a_replica_accepts(clock, monkeypatch) -> None:
    attempts = []

    class ApiNative:
        def __init__(self, endpoint, **kwargs):
            pass

        def call_external_ipc(self, name, version, payload, **kwargs):
            attempts.append(kwargs["timeout"])
            if len(attempts) < 3:
                raise RemoteCallUnavailableError("no healthy upstream")
            return [_result_ipc([42])]

        def close(self):
            pass

    monkeypatch.setattr(function_mod, "_should_call_via_api", lambda: True)
    monkeypatch.setattr("chalkcompute._remote_call_client.RemoteCallClient", ApiNative)
    fn = _remote_function()
    fn._client._api_server = "https://api.chalk.test"

    assert fn.remote(41) == 42
    assert attempts == [60, 59.75, 59.25]


def test_concurrent_calls_each_wait_for_a_replica(monkeypatch) -> None:
    monkeypatch.setattr(function_mod, "_NO_REPLICA_INITIAL_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(function_mod, "_NO_REPLICA_MAX_BACKOFF_SECONDS", 0.005)
    lock = threading.Lock()
    failures: dict[bytes, int] = {}

    class ColdClient(FakeRemoteCallClient):
        def call_ipc(self, name, input_ipc, metadata=(), **options):
            with lock:
                failures[input_ipc] = failures.get(input_ipc, 0) + 1
                if failures[input_ipc] <= 5:
                    raise RemoteCallUnavailableError(_NO_REPLICA)
            value = _read_input(input_ipc)["x"][0]
            return [_result_ipc([value + 1])]

    monkeypatch.setattr("chalkcompute._remote_call_client.RemoteCallClient", ColdClient)
    fn = _remote_function()
    fn._call_timeout = 10

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fn.remote, range(16)))

    assert results == [x + 1 for x in range(16)]
    assert sorted(failures.values()) == [6] * 16
