import pyarrow as pa
import pytest

from chalkcompute._function import FunctionVersionInfo, RemoteFunction
from chalkcompute._remote_call_client import RemoteCallPollResult


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


class FakeRemoteCallClient:
    instances: list["FakeRemoteCallClient"] = []
    direct_response_chunks: list[bytes] = []
    poll_results: list[RemoteCallPollResult] = []

    def __init__(self, endpoint: str, *, metadata, use_tls: bool | None, **kwargs) -> None:
        self.endpoint = endpoint
        self.metadata = list(metadata)
        self.use_tls = use_tls
        self.kwargs = kwargs
        self.calls: list[tuple[str, bytes, list[tuple[str, str]]]] = []
        self.enqueues: list[tuple[str, bytes, list[tuple[str, str]]]] = []
        self.purges: list[str] = []
        FakeRemoteCallClient.instances.append(self)

    def call_ipc(
        self,
        name: str,
        input_ipc: bytes,
        metadata=(),
    ) -> list[bytes]:
        self.calls.append((name, input_ipc, list(metadata)))
        return list(self.direct_response_chunks)

    def enqueue(
        self,
        name: str,
        input_ipc: bytes,
        metadata=(),
    ) -> str:
        self.enqueues.append((name, input_ipc, list(metadata)))
        return "call-1"

    def poll_next(
        self,
        call_id: str,
        cursor: str = "",
        poll_count: int = 0,
    ) -> RemoteCallPollResult:
        assert call_id == "call-1"
        return self.poll_results.pop(0)

    def purge_function(self, function_name: str) -> int:
        self.purges.append(function_name)
        return 7


@pytest.fixture(autouse=True)
def reset_fake_client(monkeypatch):
    import chalkcompute._remote_call_client as remote_call_client

    FakeRemoteCallClient.instances = []
    FakeRemoteCallClient.direct_response_chunks = []
    FakeRemoteCallClient.poll_results = []
    monkeypatch.setattr(remote_call_client, "RemoteCallClient", FakeRemoteCallClient)


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


def test_direct_call_uses_native_client_and_handler_name() -> None:
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]

    fn = _remote_function()

    assert fn(41) == 42

    client = FakeRemoteCallClient.instances[0]
    assert client.endpoint == "127.0.0.1:7777"
    assert client.use_tls is False
    assert ("authorization", "Bearer access-token") in client.metadata
    assert ("x-chalk-env-id", "env-id") in client.metadata
    assert ("x-chalk-deployment-type", "function-queue") not in client.metadata
    assert client.calls[0][0] == "handler"
    assert _read_input(client.calls[0][1]) == {"x": [41]}
    assert client.calls[0][2] == []


def test_direct_call_uses_service_auth_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("CHALK_REMOTE_AUTH_ENDPOINT", "https://api.chalk.test")
    monkeypatch.setenv("CHALK_REMOTE_AUTH_EXCHANGE_TOKEN", "exchange-token")
    monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-service-auth")
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]

    fn = _remote_function()
    fn._client = ServiceAuthOnlyConnectClient()

    assert fn(41) == 42

    client = FakeRemoteCallClient.instances[0]
    assert ("authorization", "Bearer access-token") not in client.metadata
    assert ("x-chalk-env-id", "env-service-auth") in client.metadata
    assert client.kwargs["auth_endpoint"] == "https://api.chalk.test"
    assert client.kwargs["exchange_token"] == "exchange-token"
    assert client.kwargs["requested_permissions"] == (2, 3, 11)


def test_direct_call_injects_fresh_trace_metadata_on_cached_client(monkeypatch) -> None:
    monkeypatch.setenv("CHALK_REMOTE_FUNCTION_TRACING", "1")
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

    assert fn(41) == 42
    assert fn(41) == 42

    assert len(FakeRemoteCallClient.instances) == 1
    assert (
        "traceparent",
        "00-11111111111111111111111111111111-1111111111111111-01",
    ) in FakeRemoteCallClient.instances[0].calls[0][2]
    assert (
        "traceparent",
        "00-22222222222222222222222222222222-2222222222222222-01",
    ) in FakeRemoteCallClient.instances[0].calls[1][2]
    assert (
        "x-chalk-function-name",
        "catalog-add",
    ) in FakeRemoteCallClient.instances[0].calls[0][2]


def test_direct_call_does_not_inject_trace_metadata_by_default(monkeypatch) -> None:
    def fail_trace_metadata() -> list[tuple[str, str]]:
        raise AssertionError("trace metadata should not be requested unless tracing is enabled")

    monkeypatch.setattr(
        "chalkcompute._function.current_trace_metadata",
        fail_trace_metadata,
    )
    FakeRemoteCallClient.direct_response_chunks = [_result_ipc([42])]

    fn = _remote_function()

    assert fn(41) == 42

    assert FakeRemoteCallClient.instances[0].calls[0][2] == []


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


def test_defer_injects_trace_metadata(monkeypatch) -> None:
    monkeypatch.setenv("CHALK_REMOTE_FUNCTION_TRACING", "1")
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
    assert ("x-chalk-function-name", "catalog-add") in client.enqueues[0][2]
    assert client.enqueues[0][0] == "catalog-add"


def test_queue_call_uses_service_auth_engine_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("CHALK_REMOTE_AUTH_ENDPOINT", "https://api.chalk.test")
    monkeypatch.setenv("CHALK_REMOTE_AUTH_EXCHANGE_TOKEN", "exchange-token")
    monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-service-auth")
    monkeypatch.setenv("CHALK_GRPC_ENGINE", "https://engine.chalk.test")
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
