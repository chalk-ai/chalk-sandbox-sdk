"""Exercise ambient runtime auth through the public sandbox client."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf.wkt import Timestamp

from chalkcompute import Image, SandboxClient
from chalkcompute._connect import ConnectClient, ConnectError
from chalkcompute._gen.chalk.auth.v1.permissions_pb import Permission as P
from chalkcompute._gen.chalk.sandbox.v2 import service_pb as pb
from chalkcompute._gen.chalk.server.v1 import auth_pb
from chalkcompute._remote_call_client import ServiceAuthContext

BASE = {P.AUTHENTICATED, P.QUERY_ONLINE, P.DEPLOY_READ}
PROVISION = {P.DEPLOY_CREATE, P.DEPLOY_REDEPLOY}


@pytest.fixture
def runtime(monkeypatch):
    state = SimpleNamespace(
        now=1000.0,
        requests=[],
        tokens={},
        allowed=BASE | PROVISION,
        fail=False,
        exchanges=0,
    )
    monkeypatch.delenv("CHALK_WEB_IDENTITY_TOKEN_FILE", raising=False)
    monkeypatch.setattr("chalkcompute._connect.time.time", lambda: state.now)
    monkeypatch.setattr("chalkcompute._connect.time.monotonic", lambda: state.now)
    monkeypatch.setattr(
        "chalkcompute._connect.service_auth_context_from_env",
        lambda: ServiceAuthContext(
            auth_endpoint="https://api.chalk.test",
            exchange_token="runtime-identity",
            environment_id="env-test",
            requested_permissions=tuple(sorted(BASE)),
            grpc_engine=None,
        ),
    )

    class Auth:
        def __init__(self, *args, **kwargs):
            super().__init__()

        def get_internal_working_token(self, request, **kwargs):
            state.exchanges += 1
            permissions = set(request.requested_permissions)
            state.requests.append(permissions)
            assert kwargs["headers"]["Authorization"] == "Bearer runtime-identity"
            if state.fail:
                raise ConnectRpcError(Code.UNAVAILABLE, "exchange unavailable")
            if not permissions <= state.allowed:
                raise ConnectRpcError(
                    Code.PERMISSION_DENIED, "provisioning permission denied"
                )
            token = f"token-{state.exchanges}"
            state.tokens[f"Bearer {token}"] = permissions
            return auth_pb.GetInternalWorkingTokenResponse(
                access_token=token,
                expires_at=Timestamp(seconds=int(state.now + 120)),
            )

        def close(self):
            pass

    monkeypatch.setattr(
        "chalkcompute._connect.auth_connect.AuthServiceClientSync", Auth
    )
    return state


def sandbox_stub(runtime):
    stub = Mock()

    def check(headers, permissions):
        assert headers["x-chalk-env-id"] == "env-test"
        assert permissions <= runtime.tokens[headers["authorization"]]

    def create(request, *, headers):
        check(headers, PROVISION)
        return pb.CreateSandboxResponse(
            sandbox=pb.SandboxInfo(id="sandbox-test", status=pb.SandboxStatus.RUNNING)
        )

    def get(request, *, headers):
        check(headers, {P.DEPLOY_READ})
        return pb.GetSandboxResponse(
            sandbox=pb.SandboxInfo(id=request.id, status=pb.SandboxStatus.RUNNING)
        )

    def terminate(request, *, headers):
        check(headers, {P.DEPLOY_CREATE})
        return pb.TerminateSandboxResponse()

    stub.create_sandbox.side_effect = create
    stub.get_sandbox.side_effect = get
    stub.terminate_sandbox.side_effect = terminate
    stub.list_sandboxes.return_value = pb.ListSandboxesResponse()
    return stub


def test_create_refresh_and_terminate_with_same_client(runtime):
    with SandboxClient.from_env() as client:
        stub = sandbox_stub(runtime)
        client._stub = stub
        assert runtime.requests == [BASE]
        sandbox = client.create("node:22-bookworm", wait=False)
        assert runtime.requests == [BASE, BASE | PROVISION]
        sandbox.refresh()
        assert runtime.exchanges == 2
        runtime.now += 59
        sandbox.refresh()
        assert runtime.exchanges == 2
        runtime.now += 1
        sandbox.refresh()
        assert runtime.exchanges == 3
        assert (
            stub.get_sandbox.call_args.kwargs["headers"]["authorization"]
            == "Bearer token-3"
        )
        sandbox.terminate()
        assert runtime.exchanges == 3
        stub.terminate_sandbox.assert_called_once()


def test_denied_provisioning_does_not_break_read_only_client(runtime):
    runtime.allowed = BASE
    with SandboxClient.from_env() as client:
        client._stub = sandbox_stub(runtime)
        assert client.list() == []
        assert runtime.requests == [BASE]
        with pytest.raises(
            ConnectRpcError, match="provisioning permission denied"
        ) as exc:
            client.create("node:22-bookworm", wait=False)
        assert exc.value.code == Code.PERMISSION_DENIED
        client._stub.create_sandbox.assert_not_called()
        assert client.list() == []
        assert runtime.exchanges == 2
        assert client._headers["authorization"] == "Bearer token-1"


def test_failed_refresh_does_not_send_stale_token_and_can_recover(runtime):
    with SandboxClient.from_env() as client:
        client._stub = sandbox_stub(runtime)
        sandbox = client.create("node:22-bookworm", wait=False)
        runtime.now += 121
        runtime.fail = True
        with pytest.raises(ConnectRpcError, match="exchange unavailable"):
            sandbox.refresh()
        client._stub.get_sandbox.assert_not_called()
        runtime.fail = False
        sandbox.refresh()
        assert (
            client._stub.get_sandbox.call_args.kwargs["headers"]["authorization"]
            == "Bearer token-4"
        )
        client._stub.create_sandbox.assert_called_once()


def test_termination_of_existing_sandbox_requests_only_create(runtime):
    runtime.allowed = BASE | {P.DEPLOY_CREATE}
    with SandboxClient.from_env() as client:
        client._stub = sandbox_stub(runtime)
        sandbox = client.get("sandbox-test")
        sandbox.terminate()
        assert runtime.requests == [BASE, BASE | {P.DEPLOY_CREATE}]


def test_image_build_uses_provisioning_token(runtime, monkeypatch):
    def build(connect, *args, **kwargs):
        token = dict(connect._rpc_metadata())["authorization"]
        assert PROVISION <= runtime.tokens[token]
        return "registry/image:built"

    monkeypatch.setattr("chalkcompute._sandbox.build_image", build)
    with SandboxClient.from_env() as client:
        client._stub = sandbox_stub(runtime)
        client.create(Image.debian_slim(), wait=False)
        assert runtime.requests == [BASE, BASE | PROVISION]


def test_expired_runtime_identity_does_not_fall_back_to_local_credentials(
    runtime, monkeypatch
):
    with SandboxClient.from_env() as client:
        runtime.now += 121
        monkeypatch.setattr(
            "chalkcompute._connect.service_auth_context_from_env", lambda: None
        )
        with pytest.raises(ConnectError, match="runtime identity is unavailable"):
            client.list()


def test_explicit_metadata_is_not_replaced_by_runtime_identity(runtime):
    runtime.tokens["Bearer explicit-token"] = BASE | PROVISION
    with SandboxClient(
        "https://api.chalk.test",
        metadata=[
            ("authorization", "Bearer explicit-token"),
            ("x-chalk-env-id", "env-test"),
        ],
    ) as client:
        client._stub = sandbox_stub(runtime)
        sandbox = client.create("node:22-bookworm", wait=False)
        sandbox.terminate()
        assert runtime.exchanges == 0


def test_shared_connect_client_refreshes_without_sandbox(runtime):
    client = ConnectClient()
    assert dict(client._rpc_metadata())["authorization"] == "Bearer token-1"
    runtime.now += 60
    assert dict(client._rpc_metadata())["authorization"] == "Bearer token-2"
    assert runtime.requests == [BASE, BASE]


@pytest.mark.parametrize("lifetime,refresh_after", [(20, 10), (1800, 1740)])
def test_refresh_margin_tracks_token_lifetime(
    runtime, monkeypatch, lifetime, refresh_after
):
    def exchange(self, request, **kwargs):
        runtime.exchanges += 1
        return auth_pb.GetInternalWorkingTokenResponse(
            access_token=f"token-{runtime.exchanges}",
            expires_at=Timestamp(seconds=int(runtime.now + lifetime)),
        )

    monkeypatch.setattr(
        "chalkcompute._connect.auth_connect.AuthServiceClientSync.get_internal_working_token",
        exchange,
    )
    client = ConnectClient()
    client._rpc_metadata()
    runtime.now += refresh_after - 0.1
    client._rpc_metadata()
    assert runtime.exchanges == 1
    runtime.now += 0.1
    client._rpc_metadata()
    assert runtime.exchanges == 2


@pytest.mark.parametrize(
    "access_token,expires_at,message",
    [
        ("", 1120, "no access token"),
        ("expired", 999, "expired token"),
    ],
)
def test_invalid_exchange_response_is_rejected(
    runtime, monkeypatch, access_token, expires_at, message
):
    monkeypatch.setattr(
        "chalkcompute._connect.auth_connect.AuthServiceClientSync.get_internal_working_token",
        lambda *args, **kwargs: auth_pb.GetInternalWorkingTokenResponse(
            access_token=access_token,
            expires_at=Timestamp(seconds=expires_at),
        ),
    )
    with pytest.raises(ConnectError, match=message):
        SandboxClient.from_env()
