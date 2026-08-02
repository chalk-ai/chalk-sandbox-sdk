import httpx
import pytest

from chalkcompute import _http
from chalkcompute._connect import _is_retry_safe_method

_URL = "https://api.staging.chalk.dev/chalk.container.v1.ContainerService/GetContainer"
_OTHER_URL = "https://api.chalk.ai/chalk.container.v1.ContainerService/GetContainer"


@pytest.fixture(autouse=True)
def reset_pool(monkeypatch: pytest.MonkeyPatch):
    """Isolate each test from the process-global pool, and never sleep."""
    monkeypatch.setattr(_http, "_clients", {})
    monkeypatch.setattr(_http, "_owner_pid", None)
    monkeypatch.setattr(_http.time, "sleep", lambda _seconds: None)
    yield


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    monkeypatch.setattr(_http, "_build_client", lambda: _mock_client(handler))


class TestPooling:
    def test_same_origin_shares_one_client(self, monkeypatch):
        _install(monkeypatch, lambda request: httpx.Response(200))

        assert _http.get_client(_URL) is _http.get_client(_URL)

    def test_distinct_origins_get_distinct_clients(self, monkeypatch):
        _install(monkeypatch, lambda request: httpx.Response(200))

        assert _http.get_client(_URL) is not _http.get_client(_OTHER_URL)

    def test_path_does_not_affect_pooling(self, monkeypatch):
        _install(monkeypatch, lambda request: httpx.Response(200))

        first = _http.get_client("https://api.staging.chalk.dev/one")
        second = _http.get_client("https://api.staging.chalk.dev/two")

        assert first is second

    def test_fork_rebuilds_the_pool(self, monkeypatch):
        _install(monkeypatch, lambda request: httpx.Response(200))
        parent_client = _http.get_client(_URL)

        # A forked child inherits the parent's sockets and must not reuse them.
        monkeypatch.setattr(_http.os, "getpid", lambda: 424242)
        child_client = _http.get_client(_URL)

        assert child_client is not parent_client

    def test_close_all_in_child_does_not_close_parent_sockets(self, monkeypatch):
        _install(monkeypatch, lambda request: httpx.Response(200))
        parent_client = _http.get_client(_URL)

        monkeypatch.setattr(_http.os, "getpid", lambda: 424242)
        _http.close_all()

        assert not parent_client.is_closed

    def test_pooling_disabled_bypasses_the_pool(self, monkeypatch):
        monkeypatch.setenv("CHALK_SANDBOX_CONNECTION_POOLING", "0")
        sent: list[str] = []

        def fake_request(method, url, **kwargs):
            sent.append(f"{method} {url}")
            return httpx.Response(200)

        monkeypatch.setattr(_http.httpx, "request", fake_request)

        _http.request("POST", _URL, retry_safe=True)

        assert sent == [f"POST {_URL}"]
        assert _http._clients == {}


class TestEnvFlags:
    def test_http2_off_by_default(self, monkeypatch):
        # httpx's sync h2 backend races on stream-id allocation across threads,
        # which staging rejects with GOAWAY(PROTOCOL_ERROR,
        # "InvalidNewStreamId") from 8 threads up. See CHA-10029.
        monkeypatch.delenv("CHALK_SANDBOX_HTTP2", raising=False)

        assert _http.http2_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
    def test_http2_can_be_opted_into(self, monkeypatch, value):
        monkeypatch.setenv("CHALK_SANDBOX_HTTP2", value)

        assert _http.http2_enabled() is True

    def test_real_client_uses_http1_by_default(self, monkeypatch):
        monkeypatch.delenv("CHALK_SANDBOX_HTTP2", raising=False)

        client = _http._build_client()
        try:
            assert client._transport._pool._http2 is False
        finally:
            client.close()

    def test_real_client_negotiates_http2_when_opted_in(self, monkeypatch):
        monkeypatch.setenv("CHALK_SANDBOX_HTTP2", "1")

        client = _http._build_client()
        try:
            assert client._transport._pool._http2 is True
        finally:
            client.close()

    def test_keepalive_expiry_outlives_a_poll_loop(self, monkeypatch):
        monkeypatch.delenv("CHALK_SANDBOX_KEEPALIVE_EXPIRY", raising=False)

        client = _http._build_client()
        try:
            # httpx defaults to 5s, short enough that a container poll loop
            # re-dials between iterations.
            assert client._transport._pool._keepalive_expiry == 60.0
        finally:
            client.close()

    def test_non_numeric_override_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CHALK_SANDBOX_KEEPALIVE_EXPIRY", "banana")

        assert _http._env_number("CHALK_SANDBOX_KEEPALIVE_EXPIRY", 60.0) == 60.0


class TestRetries:
    def _flaky(self, error: Exception, failures: int):
        state = {"calls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            state["calls"] += 1
            if state["calls"] <= failures:
                raise error
            return httpx.Response(200)

        return handler, state

    def test_connect_error_is_retried_even_when_not_retry_safe(self, monkeypatch):
        # The request never reached the server, so re-issuing it cannot
        # duplicate a side effect.
        handler, state = self._flaky(httpx.ConnectError("nope"), failures=2)
        _install(monkeypatch, handler)

        resp = _http.request("POST", _URL, retry_safe=False)

        assert resp.status_code == 200
        assert state["calls"] == 3

    def test_disconnect_is_retried_for_a_safe_method(self, monkeypatch):
        handler, state = self._flaky(
            httpx.RemoteProtocolError("Server disconnected"), failures=1
        )
        _install(monkeypatch, handler)

        resp = _http.request("POST", _URL, retry_safe=True)

        assert resp.status_code == 200
        assert state["calls"] == 2

    def test_disconnect_is_not_retried_for_an_unsafe_method(self, monkeypatch):
        # A retried CreateContainer would leak a second container, so an
        # ambiguous failure has to surface.
        handler, state = self._flaky(
            httpx.RemoteProtocolError("Server disconnected"), failures=1
        )
        _install(monkeypatch, handler)

        with pytest.raises(httpx.RemoteProtocolError):
            _http.request("POST", _URL, retry_safe=False)

        assert state["calls"] == 1

    def test_retries_are_bounded(self, monkeypatch):
        handler, state = self._flaky(httpx.ConnectError("nope"), failures=99)
        _install(monkeypatch, handler)

        with pytest.raises(httpx.ConnectError):
            _http.request("POST", _URL, retry_safe=True)

        assert state["calls"] == _http._MAX_RETRY_ATTEMPTS

    def test_http_errors_are_not_retried(self, monkeypatch):
        state = {"calls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            state["calls"] += 1
            return httpx.Response(500)

        _install(monkeypatch, handler)

        resp = _http.request("POST", _URL, retry_safe=True)

        assert resp.status_code == 500
        assert state["calls"] == 1


class TestRetrySafeClassification:
    @pytest.mark.parametrize(
        "method",
        [
            "GetContainer",
            "GetCustomImage",
            "GetScalingGroup",
            "GetExternalFunctionVersion",
            "ListContainers",
            "ListModelScalingGroups",
            # Keyed on the image spec and returns the cached build when one
            # exists, so repeating it is free of side effects.
            "GetOrBuildCustomImage",
        ],
    )
    def test_reads_are_retry_safe(self, method):
        assert _is_retry_safe_method(method) is True

    @pytest.mark.parametrize(
        "method",
        [
            "CreateContainer",
            "StopContainer",
            "ExecCommand",
            "DeleteScalingGroup",
            "CreateExternalFunctionVersion",
            "CallExternalFunction",
        ],
    )
    def test_writes_are_not_retry_safe(self, method):
        assert _is_retry_safe_method(method) is False
