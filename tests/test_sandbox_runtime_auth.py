"""Credentials follow the invocation, including when clients are reused."""

import asyncio
import contextlib
import contextvars
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf.wkt import Timestamp

from chalkcompute import SandboxClient
from chalkcompute._call_context import (
    FUNCTION_INVOCATION_TOKEN_HEADER,
    current_invocation,
    get_call_context,
    reset_call_scope,
    set_call_scope,
)
from chalkcompute._connect import ConnectClient, ConnectError
from chalkcompute._gen.chalk.sandbox.v2 import service_pb as pb
from chalkcompute._gen.chalk.server.v1 import auth_pb


@contextlib.contextmanager
def invocation(identity):
    token = set_call_scope(
        {"metadata": {FUNCTION_INVOCATION_TOKEN_HEADER: identity}}, None
    )
    try:
        yield
    finally:
        reset_call_scope(token)


@pytest.fixture
def runtime(monkeypatch):
    state = SimpleNamespace(now=1000.0, exchanges=[], fail=False)
    monkeypatch.setenv("CHALK_FUNCTION_AUTH_MODE", "caller")
    monkeypatch.setenv("CHALK_API_SERVER", "https://api.chalk.test")
    monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-test")
    monkeypatch.setenv("CHALK_REMOTE_AUTH_EXCHANGE_TOKEN", "forbidden-ambient-token")
    monkeypatch.setattr("chalkcompute._connect.time.time", lambda: state.now)
    monkeypatch.setattr("chalkcompute._connect.time.monotonic", lambda: state.now)
    monkeypatch.setattr(
        "chalkcompute._connect.resolve_service_auth_context",
        Mock(side_effect=AssertionError("ambient identity used")),
    )
    monkeypatch.setattr(
        "chalkcompute._connect.load_token",
        Mock(side_effect=AssertionError("local credentials used")),
    )

    class Auth:
        def __init__(self, *args, **kwargs):
            super().__init__()

        def get_internal_working_token(self, request, *, headers, **kwargs):
            assert list(request.requested_permissions) == []
            identity = headers["Authorization"].removeprefix("Bearer ")
            state.exchanges.append(identity)
            if state.fail:
                raise ConnectRpcError(Code.UNAVAILABLE, "exchange unavailable")
            return auth_pb.GetInternalWorkingTokenResponse(
                access_token=f"{identity}-{state.now}",
                expires_at=Timestamp(seconds=int(state.now + 120)),
            )

        def close(self):
            pass

    monkeypatch.setattr(
        "chalkcompute._connect.auth_connect.AuthServiceClientSync", Auth
    )
    return state


def test_same_sandbox_client_switches_callers_and_refreshes(runtime):
    with invocation("alice"):
        client = SandboxClient.from_env()
        assert client._headers["authorization"] == "Bearer alice-1000.0"
        assert len(runtime.exchanges) == 1
        runtime.now += 60
        assert client._headers["authorization"] == "Bearer alice-1060.0"
        assert len(runtime.exchanges) == 2
    with invocation("bob"):
        assert client._headers["authorization"] == "Bearer bob-1060.0"
    with pytest.raises(ConnectError, match="no delegated caller"):
        _ = client._headers
    client.close()


def test_concurrent_callers_on_shared_connect_client(runtime):
    client = ConnectClient()
    barrier = Barrier(2)

    def call(identity):
        with invocation(identity):
            first = dict(client._rpc_metadata())["authorization"]
            barrier.wait(timeout=5)
            second = dict(client._rpc_metadata())["authorization"]
            return first, second

    with ThreadPoolExecutor(max_workers=2) as executor:
        alice = executor.submit(call, "alice")
        bob = executor.submit(call, "bob")
        assert alice.result() == ("Bearer alice-1000.0",) * 2
        assert bob.result() == ("Bearer bob-1000.0",) * 2
    assert sorted(runtime.exchanges) == ["alice", "bob"]


def test_async_callers_are_isolated(runtime):
    client = ConnectClient()

    async def call(identity):
        with invocation(identity):
            first = dict(client._rpc_metadata())["authorization"]
            await asyncio.sleep(0)
            return first, dict(client._rpc_metadata())["authorization"]

    async def run():
        return await asyncio.gather(call("alice"), call("bob"))

    assert asyncio.run(run()) == [
        ("Bearer alice-1000.0",) * 2,
        ("Bearer bob-1000.0",) * 2,
    ]


def test_missing_delegation_never_uses_worker_identity(runtime):
    with pytest.raises(ConnectError, match="no delegated caller"):
        set_call_scope({"metadata": {"traceparent": "trace"}}, None)
    assert current_invocation() is None
    assert runtime.exchanges == []


def test_delegation_is_not_exposed_in_public_call_context(runtime):
    with invocation("alice"):
        assert current_invocation().exchange_token == "alice"
        assert FUNCTION_INVOCATION_TOKEN_HEADER not in get_call_context()


def test_finished_invocation_cannot_be_reused_in_background(runtime):
    with invocation("alice"):
        copied = contextvars.copy_context()
    with pytest.raises(RuntimeError, match="no longer active"):
        copied.run(ConnectClient()._rpc_metadata)


def test_refresh_failure_sends_no_sandbox_request(runtime):
    with invocation("alice"):
        with SandboxClient.from_env() as client:
            client._stub = Mock()
            runtime.now += 121
            runtime.fail = True
            with pytest.raises(ConnectRpcError, match="exchange unavailable"):
                client.list()
            client._stub.list_sandboxes.assert_not_called()
            runtime.fail = False
            client._stub.list_sandboxes.return_value = pb.ListSandboxesResponse()
            assert client.list() == []


def test_read_only_caller_cannot_provision(runtime):
    with invocation("read-only"):
        with SandboxClient.from_env() as client:
            client._stub = Mock()
            client._stub.list_sandboxes.return_value = pb.ListSandboxesResponse()
            assert client.list() == []

            def deny(request, *, headers):
                assert headers["authorization"].startswith("Bearer read-only-")
                raise ConnectRpcError(
                    Code.PERMISSION_DENIED, "caller cannot create sandboxes"
                )

            client._stub.create_sandbox.side_effect = deny
            with pytest.raises(ConnectRpcError, match="caller cannot create"):
                client.create("node:22-bookworm", wait=False)
            assert runtime.exchanges == ["read-only"]


def test_direct_metadata_remains_explicit(runtime):
    with SandboxClient(
        "https://api.chalk.test", metadata=[("authorization", "Bearer explicit")]
    ) as client:
        with invocation("alice"):
            assert client._headers["authorization"] == "Bearer explicit"
    assert runtime.exchanges == ["alice"]


def test_delegation_header_is_case_insensitive(runtime):
    scope = set_call_scope(
        {"metadata": {"X-Chalk-Function-Invocation-Token": "alice"}}, None
    )
    try:
        assert current_invocation().exchange_token == "alice"
        assert not get_call_context()
    finally:
        reset_call_scope(scope)


def test_caller_identity_takes_precedence_over_ambient_web_identity(
    runtime, monkeypatch
):
    monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", "/nonexistent/worker-token")
    with invocation("alice"):
        assert (
            dict(ConnectClient()._rpc_metadata())["authorization"]
            == "Bearer alice-1000.0"
        )


def test_static_chalkpy_client_cannot_capture_caller(runtime):
    from chalkcompute._connect import chalk_client_from_env

    with invocation("alice"):
        with pytest.raises(ConnectError, match="invocation-scoped"):
            chalk_client_from_env()


def test_service_mode_requires_explicit_credentials(monkeypatch):
    monkeypatch.setenv("CHALK_FUNCTION_AUTH_MODE", "service")
    for name in (
        "CHALK_CLIENT_ID",
        "CHALK_CLIENT_SECRET",
        "CHALK_WEB_IDENTITY_TOKEN_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConnectError, match="explicit service credentials"):
        set_call_scope({}, None)
    assert current_invocation() is None


def test_function_metadata_never_forwards_original_credentials(monkeypatch):
    client = ConnectClient()
    client._access_token = "original-caller"
    client._env_id = "env-test"
    client._api_server = "https://api.chalk.test"
    monkeypatch.setattr(client, "_ensure_auth", lambda: None)
    seen = []

    class Auth:
        def create_function_invocation_token(self, request, *, headers, **kwargs):
            assert headers["authorization"] == "Bearer original-caller"
            assert request.function_name == "provision"
            seen.append("delegate")
            return auth_pb.CreateFunctionInvocationTokenResponse(exchange_token="seed")

        def get_internal_working_token(self, request, *, headers, **kwargs):
            assert headers["authorization"] == "Bearer seed"
            assert not request.requested_permissions
            seen.append("exchange")
            return auth_pb.GetInternalWorkingTokenResponse(access_token="working")

    monkeypatch.setattr(client, "stub", lambda stub: Auth())
    assert dict(client.function_invocation_metadata("provision")) == {
        FUNCTION_INVOCATION_TOKEN_HEADER: "seed",
        "authorization": "Bearer working",
    }
    assert seen == ["delegate", "exchange"]
