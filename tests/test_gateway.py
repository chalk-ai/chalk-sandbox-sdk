"""Exercise real OpenAI requests against an in-memory transport; no credentials needed."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar

import pytest

import chalkcompute as cc
from chalkcompute import _gateway

openai = pytest.importorskip("openai")
httpx2 = pytest.importorskip("httpx2")


@pytest.fixture
def gateway(monkeypatch):
    requests = []
    state = {"token": "first", "connections": 0, "auth_calls": 0}
    session = ContextVar("gateway_test_session", default=None)

    class Connection:
        _api_server = "https://chalk.example/"
        _env_id = "demo"

        def __init__(self):
            state["connections"] += 1

        def _ensure_auth(self):
            state["auth_calls"] += 1
            self._access_token = state["token"]

        def close(self):
            pass

    def respond(request):
        requests.append(request)
        if state.pop("retry_once", False):
            state["token"] = "rotated"
            return httpx2.Response(
                500, headers={"retry-after-ms": "1"}, json={"error": "retry"}
            )
        if state.pop("redirect", False):
            return httpx2.Response(
                302, headers={"location": "https://elsewhere.example/v1/models"}
            )
        return httpx2.Response(200, json={"object": "list", "data": []})

    original_client = openai.DefaultHttpxClient
    monkeypatch.setattr(
        openai,
        "DefaultHttpxClient",
        lambda **kwargs: original_client(
            transport=httpx2.MockTransport(respond), **kwargs
        ),
    )
    monkeypatch.setattr(_gateway, "ConnectClient", Connection)
    monkeypatch.setattr(_gateway._tracing, "current_session_id", session.get)
    monkeypatch.setattr(
        _gateway._tracing,
        "trace_headers",
        lambda: {"traceparent": f"trace-{session.get()}"} if session.get() else {},
    )
    return requests, state, session


def test_public_client_uses_chalk_endpoint_credentials_and_sdk_defaults(
    gateway, monkeypatch
):
    requests, _, _ = gateway
    monkeypatch.setenv("OPENAI_API_KEY", "provider-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://other.example/v1")
    with cc.openai_client() as client:
        assert client.max_retries == openai.DEFAULT_MAX_RETRIES
        assert client.timeout == openai.DEFAULT_TIMEOUT
        client.models.list()
    assert client.is_closed()
    request = requests[0]
    assert str(request.url) == "https://chalk.example/v1/router/models"
    assert request.headers["Authorization"] == "Bearer first"
    assert request.headers["X-Chalk-Env-Id"] == "demo"


def test_reused_client_refreshes_identity_and_does_not_retain_row_context(gateway):
    requests, state, session = gateway
    with cc.openai_client() as client:
        token = session.set("row-1")
        client.models.list()
        session.reset(token)
        state["token"] = "rotated"
        token = session.set("row-2")
        client.models.list()
        session.reset(token)
        client.models.list()
    assert [r.headers["Authorization"] for r in requests] == [
        "Bearer first",
        "Bearer rotated",
        "Bearer rotated",
    ]
    assert [r.headers.get("X-Chalk-Agent-Session-Id") for r in requests] == [
        "row-1",
        "row-2",
        None,
    ]
    assert [r.headers.get("traceparent") for r in requests] == [
        "trace-row-1",
        "trace-row-2",
        None,
    ]
    assert state["auth_calls"] == 4  # construction, then each request


def test_long_lived_client_replaces_cached_credential(gateway, monkeypatch):
    _, state, _ = gateway
    clock = [1000.0]
    monkeypatch.setattr(_gateway.time, "monotonic", lambda: clock[0])
    with cc.openai_client() as client:
        client.models.list()
        clock[0] += _gateway._REFRESH_SECONDS
        client.models.list()
    assert state["connections"] == 2


def test_shared_client_keeps_concurrent_row_context_isolated(gateway):
    requests, _, session = gateway
    with cc.openai_client() as client:

        def call(row):
            token = session.set(row)
            try:
                client.models.list()
            finally:
                session.reset(token)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(call, [f"row-{i}" for i in range(8)]))
    assert {r.headers["X-Chalk-Agent-Session-Id"] for r in requests} == {
        f"row-{i}" for i in range(8)
    }
    assert all(
        r.headers["traceparent"] == f"trace-{r.headers['X-Chalk-Agent-Session-Id']}"
        for r in requests
    )


def test_client_copy_cannot_send_chalk_credentials_to_another_endpoint(gateway):
    requests, _, _ = gateway
    with cc.openai_client() as client:
        with pytest.raises(ValueError, match="only send requests"):
            client.with_options(base_url="https://elsewhere.example/v1").models.list()
    assert requests == []


@pytest.mark.parametrize("option", sorted(_gateway._MANAGED_OPTIONS))
def test_rejects_auth_and_endpoint_overrides_before_resolving_credentials(option):
    with pytest.raises(TypeError, match="Chalk manages"):
        cc.openai_client(**{option: "override"})


def test_reports_missing_chalk_credentials_without_provider_fallback(
    gateway, monkeypatch
):
    def unavailable():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(_gateway, "ConnectClient", unavailable)
    with pytest.raises(RuntimeError, match="Chalk gateway authentication failed"):
        cc.openai_client()


def test_retry_resolves_current_credential_again(gateway):
    requests, state, _ = gateway
    state["retry_once"] = True
    with cc.openai_client() as client:
        client.models.list()
    assert [r.headers["Authorization"] for r in requests] == [
        "Bearer first",
        "Bearer rotated",
    ]


def test_redirects_do_not_forward_credentials(gateway):
    requests, state, _ = gateway
    state["redirect"] = True
    with cc.openai_client() as client:
        with pytest.raises(openai.APIStatusError):
            client.models.list()
    assert len(requests) == 1
