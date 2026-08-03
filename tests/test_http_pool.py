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


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    monkeypatch.setattr(
        _http, "_build_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )


class TestPooling:
    def test_same_origin_shares_one_client(self, monkeypatch):
        _install(monkeypatch, lambda request: httpx.Response(200))

        assert _http.get_client(_URL) is _http.get_client(_URL)

    def test_distinct_origins_get_distinct_clients(self, monkeypatch):
        _install(monkeypatch, lambda request: httpx.Response(200))

        assert _http.get_client(_URL) is not _http.get_client(_OTHER_URL)

    def test_path_does_not_affect_pooling(self, monkeypatch):
        _install(monkeypatch, lambda request: httpx.Response(200))

        assert _http.get_client("https://api.staging.chalk.dev/one") is _http.get_client(
            "https://api.staging.chalk.dev/two"
        )

    def test_fork_rebuilds_the_pool(self, monkeypatch):
        _install(monkeypatch, lambda request: httpx.Response(200))
        parent_client = _http.get_client(_URL)

        # A forked child inherits the parent's sockets and must not reuse them.
        monkeypatch.setattr(_http.os, "getpid", lambda: 424242)

        assert _http.get_client(_URL) is not parent_client

    def test_close_all_in_child_does_not_close_parent_sockets(self, monkeypatch):
        _install(monkeypatch, lambda request: httpx.Response(200))
        parent_client = _http.get_client(_URL)

        monkeypatch.setattr(_http.os, "getpid", lambda: 424242)
        _http.close_all()

        assert not parent_client.is_closed

    def test_connection_ceiling_is_what_bounds_descriptors(self):
        client = _http._build_client()
        try:
            pool = client._transport._pool
            assert pool._max_connections == _http._MAX_CONNECTIONS
            # httpx defaults to 5s, short enough that a container poll loop
            # re-dials between iterations.
            assert pool._keepalive_expiry == 60.0
        finally:
            client.close()

    def test_pooling_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("CHALK_SANDBOX_CONNECTION_POOLING", "0")
        sent: list[str] = []

        def fake_request(method, url, **kwargs):
            sent.append(f"{method} {url}")
            return httpx.Response(200)

        monkeypatch.setattr(_http.httpx, "request", fake_request)

        _http.request("POST", _URL, retry_safe=True)

        assert sent == [f"POST {_URL}"]
        assert _http._clients == {}


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

        assert _http.request("POST", _URL, retry_safe=False).status_code == 200
        assert state["calls"] == 3

    def test_disconnect_is_retried_for_a_safe_method(self, monkeypatch):
        handler, state = self._flaky(
            httpx.RemoteProtocolError("Server disconnected"), failures=1
        )
        _install(monkeypatch, handler)

        assert _http.request("POST", _URL, retry_safe=True).status_code == 200
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

        assert _http.request("POST", _URL, retry_safe=True).status_code == 500
        assert state["calls"] == 1


class TestRetrySafeClassification:
    @pytest.mark.parametrize(
        "method",
        [
            "GetContainer",
            "GetCustomImage",
            "GetScalingGroup",
            "ListContainers",
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
            "CallExternalFunction",
        ],
    )
    def test_writes_are_not_retry_safe(self, method):
        assert _is_retry_safe_method(method) is False
