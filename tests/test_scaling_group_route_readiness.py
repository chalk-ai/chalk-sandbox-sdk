from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import chalkcompute._backoff as backoff_mod
import chalkcompute._scaling_group as scaling_group_mod
from chalkcompute import ReadinessProbe, ScalingGroup, StartupProbe
from tests._fakes import ScalingGroupClient


class Clock:
    """Advance time without sleeping so the full retry deadline is tested."""

    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay


class StreamResponse:
    """Track body reads and cleanup without opening a network connection."""

    def __init__(
        self,
        status: int,
        chunks: Iterable[bytes | bytearray | memoryview | Exception],
    ) -> None:
        self.status = status
        self.chunks = chunks
        self.content_accesses = 0
        self.chunks_read = 0
        self.closed = False

    @property
    def content(self) -> Iterator[bytes | bytearray | memoryview]:
        self.content_accesses += 1
        return self._read_chunks()

    def _read_chunks(self) -> Iterator[bytes | bytearray | memoryview]:
        for chunk in self.chunks:
            self.chunks_read += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def __enter__(self) -> StreamResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self.closed = True


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(scaling_group_mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(scaling_group_mod.time, "sleep", clock.sleep)
    monkeypatch.setattr(
        backoff_mod.random, "uniform", lambda low, high: (low + high) / 2
    )
    return clock


@pytest.fixture
def route_transport(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    constructor = MagicMock()
    monkeypatch.setattr(scaling_group_mod, "SyncHTTPTransport", constructor)
    return constructor


@pytest.fixture
def route_client(
    monkeypatch: pytest.MonkeyPatch, route_transport: MagicMock
) -> MagicMock:
    constructor = MagicMock()
    monkeypatch.setattr(scaling_group_mod, "SyncClient", constructor)
    return constructor.return_value


@pytest.fixture
def http_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    monkeypatch.setattr(scaling_group_mod, "SYNC_HTTP_CLIENT", client)
    return client


@pytest.mark.parametrize("authenticated", [False, True])
def test_deploy_retries_gateway_errors_then_allows_first_call(
    clock: Clock,
    route_client: MagicMock,
    route_transport: MagicMock,
    http_client: MagicMock,
    authenticated: bool,
) -> None:
    unavailable = StreamResponse(503, [b"upstream connect error"])
    ready = StreamResponse(200, [b"root body need not be read"])
    route_client.stream.side_effect = [TimeoutError("offline"), unavailable, ready]
    group = ScalingGroup(image="nginx:alpine", authenticated=authenticated)
    group._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    assert group.deploy() is group

    assert clock.delays == [0.25, 0.5]
    assert route_client.stream.call_count == 3
    for call in route_client.stream.call_args_list:
        assert call.args == ("GET", "https://example.com/")
        assert call.kwargs["headers"] == (
            {"Authorization": "Bearer test-token"} if authenticated else None
        )
        assert call.kwargs["timeout"] == 5
    assert unavailable.closed and ready.closed
    assert ready.content_accesses == 0
    route_transport.assert_called_once_with(
        http_version=scaling_group_mod.SYNC_HTTP_VERSION,
        tls_include_system_certs=True,
        follow_redirects=False,
    )
    cast(MagicMock, scaling_group_mod.SyncClient).assert_called_once_with(
        transport=route_transport.return_value.__enter__.return_value
    )
    route_transport.return_value.__exit__.assert_called_once()

    # The deployment probe must leave ordinary calls on the shared client.
    http_client.execute.assert_not_called()
    http_client.execute.return_value = MagicMock(status=200, content=b"first real call")
    assert group.call("/work", method="GET", cold_start_timeout=0).content == (
        b"first real call"
    )
    http_client.execute.assert_called_once()
    assert http_client.execute.call_args.args == ("GET", "https://example.com/work")


@pytest.mark.parametrize("jitter_fraction", [0.0, 1.0])
def test_route_wait_jitters_and_caps_nominal_backoff(
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    route_client: MagicMock,
    jitter_fraction: float,
) -> None:
    bounds: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        bounds.append((low, high))
        return low + jitter_fraction * (high - low)

    monkeypatch.setattr(backoff_mod.random, "uniform", uniform)
    route_client.stream.side_effect = [
        *(StreamResponse(503, [b"no healthy upstream"]) for _ in range(5)),
        StreamResponse(200, []),
    ]
    group = ScalingGroup(image="nginx:alpine")
    group._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    group.deploy()

    steps = [0.25, 0.5, 1.0, 2.0, 2.0]
    assert clock.delays == pytest.approx(
        [step * (0.8 + 0.4 * jitter_fraction) for step in steps]
    )
    assert [low for low, _ in bounds] == pytest.approx([0.8 * step for step in steps])
    assert [high for _, high in bounds] == pytest.approx([1.2 * step for step in steps])
    assert route_client.stream.call_count == 6


@pytest.mark.parametrize("status", [200, 301, 302, 401, 404, 405, 500])
def test_application_headers_finish_without_reading_the_body(
    clock: Clock, route_client: MagicMock, status: int
) -> None:
    response = StreamResponse(status, [TimeoutError("body never finishes")])
    route_client.stream.return_value = response
    group = ScalingGroup(image="nginx:alpine")
    group._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    group.deploy()

    assert route_client.stream.call_count == 1
    assert response.content_accesses == 0
    assert response.closed
    assert clock.now == 0
    assert clock.delays == []


def test_empty_application_404_finishes_immediately(
    clock: Clock, route_client: MagicMock
) -> None:
    response = StreamResponse(404, [])
    route_client.stream.return_value = response
    group = ScalingGroup(image="nginx:alpine")
    group._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    group.deploy()

    assert route_client.stream.call_count == 1
    assert response.content_accesses == 0
    assert response.closed
    assert clock.now == 0


def test_gateway_marker_split_across_chunks_is_retried(
    clock: Clock, route_client: MagicMock
) -> None:
    first = b"upstream con"
    second = bytearray(b"nect error".ljust(512 - len(first), b" "))
    unavailable = StreamResponse(503, [memoryview(first), second, b"do not read"])
    ready = StreamResponse(200, [])
    route_client.stream.side_effect = [unavailable, ready]
    group = ScalingGroup(image="nginx:alpine")
    group._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    group.deploy()

    assert route_client.stream.call_count == 2
    assert unavailable.chunks_read == 2
    assert unavailable.closed and ready.closed
    assert clock.delays == [0.25]


@pytest.mark.parametrize(
    "chunks",
    [
        [],
        [b"maintenance"],
        [b"x" * 512, b"upstream connect error"],
        [b"x" * 512 + b"upstream connect error", b"do not read"],
    ],
)
def test_unrecognized_503_reads_only_a_bounded_prefix_then_returns(
    clock: Clock, route_client: MagicMock, chunks: list[bytes]
) -> None:
    response = StreamResponse(503, chunks)
    route_client.stream.return_value = response
    group = ScalingGroup(image="nginx:alpine")
    group._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    group.deploy()

    assert route_client.stream.call_count == 1
    assert response.chunks_read == min(1, len(chunks))
    assert response.closed
    assert clock.now == 0
    assert clock.delays == []


@pytest.mark.parametrize("error", [TimeoutError("slow body"), ConnectionError("reset")])
def test_503_body_read_error_returns_without_restarting_the_retry_budget(
    clock: Clock, route_client: MagicMock, error: Exception
) -> None:
    response = StreamResponse(503, [error])
    route_client.stream.return_value = response
    group = ScalingGroup(image="nginx:alpine")
    group._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    group.deploy()

    assert route_client.stream.call_count == 1
    assert response.closed
    assert clock.now == 0
    assert clock.delays == []


@pytest.mark.parametrize("failure", ["gateway", "connection", "timeout"])
@pytest.mark.parametrize("request_seconds", [0.0, 5.0])
@pytest.mark.parametrize("jitter_fraction", [0.0, 1.0])
@pytest.mark.parametrize(
    "readiness_probe", [None, ReadinessProbe.http("/health", timeout_seconds=5)]
)
def test_route_wait_warns_and_returns_at_twenty_second_deadline(
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    route_client: MagicMock,
    route_transport: MagicMock,
    caplog: pytest.LogCaptureFixture,
    failure: str,
    request_seconds: float,
    jitter_fraction: float,
    readiness_probe: ReadinessProbe | None,
) -> None:
    requests: list[tuple[float, float]] = []
    responses: list[StreamResponse] = []
    monkeypatch.setattr(
        backoff_mod.random,
        "uniform",
        lambda low, high: low + jitter_fraction * (high - low),
    )

    def request(*args: Any, timeout: float, **kwargs: Any) -> StreamResponse:
        requests.append((clock.now, timeout))
        clock.now += min(request_seconds, timeout)
        if failure == "connection":
            raise ConnectionError("offline")
        if failure == "timeout":
            raise TimeoutError("offline")
        response = StreamResponse(503, [b"no healthy upstream"])
        responses.append(response)
        return response

    route_client.stream.side_effect = request
    group = ScalingGroup(
        image="nginx:alpine", name="route-timeout", readiness_probe=readiness_probe
    )
    group._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    assert group.deploy() is group

    assert all(0 < timeout <= min(5, 20 - started) for started, timeout in requests)
    assert clock.now == 20
    assert max(clock.delays) <= 2.4
    assert all(response.closed for response in responses)
    route_transport.return_value.__exit__.assert_called_once()
    assert "route-timeout route was not ready after 20.0s" in caplog.text


def test_route_transport_setup_failure_warns_and_returns(
    clock: Clock,
    route_client: MagicMock,
    route_transport: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    route_transport.side_effect = ValueError("transport unavailable")
    group = ScalingGroup(image="nginx:alpine", name="route-setup-error")
    group._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    assert group.deploy() is group

    route_client.stream.assert_not_called()
    assert clock.now == 0
    assert clock.delays == []
    assert "route-setup-error route check skipped (ValueError)" in caplog.text


def test_unexpected_request_error_warns_without_retrying(
    clock: Clock,
    route_client: MagicMock,
    route_transport: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    route_client.stream.side_effect = ValueError("invalid request")
    group = ScalingGroup(image="nginx:alpine", name="route-request-error")
    group._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    assert group.deploy() is group

    assert route_client.stream.call_count == 1
    route_transport.return_value.__exit__.assert_called_once()
    assert clock.now == 0
    assert clock.delays == []
    assert "route-request-error route check skipped (ValueError)" in caplog.text


@pytest.mark.parametrize(
    ("options", "web_url"),
    [
        ({"min_replicas": 0}, "https://example.com"),
        ({"protocol": "grpc"}, "https://example.com"),
        ({"port": None}, None),
        ({}, None),
        ({"startup_probe": StartupProbe.http("/health")}, "https://example.com"),
    ],
)
def test_deploy_skips_unnecessary_route_wait(
    route_client: MagicMock,
    route_transport: MagicMock,
    http_client: MagicMock,
    options: dict[str, Any],
    web_url: str | None,
) -> None:
    group = ScalingGroup(image="nginx:alpine", **options)
    group._client = cast(Any, ScalingGroupClient(web_url=web_url))

    group.deploy()

    route_transport.assert_not_called()
    route_client.stream.assert_not_called()
    http_client.execute.assert_not_called()
