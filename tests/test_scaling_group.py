from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import chalkcompute._scaling_group as scaling_group_mod
from chalkcompute import ExecutionBackend, NetworkPolicy, ScalingGroup
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from tests._fakes import ScalingGroupClient


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (None, None),
        (NetworkPolicy(), container_pb.NetworkPolicy()),
        (
            NetworkPolicy(
                allowed_routes=[NetworkPolicy.Route("10.0.0.0/8", ports=[443])],
                denied_routes=["10.1.0.0/16"],
                allowed_hosts={"example.com": []},
            ),
            container_pb.NetworkPolicy(
                allowed_routes=[
                    container_pb.AllowedRoute(
                        route="10.0.0.0/8",
                        port_ranges=[
                            container_pb.PortRange(start_port=443, end_port=443)
                        ],
                    )
                ],
                denied_routes=["10.1.0.0/16"],
                allowed_hosts={"example.com": container_pb.NetworkPolicyRuleList()},
            ),
        ),
    ],
)
def test_scaling_group_deploy_forwards_spec_fields(
    policy: NetworkPolicy | None,
    expected: container_pb.NetworkPolicy | None,
) -> None:
    client = ScalingGroupClient()
    sg = ScalingGroup(
        image="nginx:alpine",
        name="tagged-group",
        tags={"team": "compute"},
        chalk_identity=True,
        network_policy=policy,
    )
    sg._client = cast(Any, client)

    sg.deploy()

    request = client.requests[0]
    assert isinstance(request, sg_pb.CreateScalingGroupRequest)
    assert request.spec is not None and request.spec.container_spec is not None
    assert dict(request.spec.container_spec.tags) == {"team": "compute"}
    assert request.spec.container_spec.has_field("chalk_workload_identity")
    assert request.spec.container_spec.has_field("network_policy") == (
        expected is not None
    )
    assert request.spec.container_spec.network_policy == expected


@pytest.mark.parametrize(
    ("json", "data", "expected"),
    [
        ({"hello": "world"}, None, {"hello": "world"}),
        ({"ignored": True}, b"raw", b"raw"),
    ],
)
def test_scaling_group_call_sends_pyqwest_content(
    monkeypatch: pytest.MonkeyPatch,
    json: dict[str, object],
    data: bytes | None,
    expected: object,
) -> None:
    client = ScalingGroupClient()
    sg = ScalingGroup(image="nginx:alpine", name="content-test")
    sg._client = cast(Any, client)
    sg.deploy()
    assert sg._scaling_group_info is not None
    sg._scaling_group_info.web_url = "https://example.com"
    http_client = MagicMock()
    monkeypatch.setattr(scaling_group_mod, "SYNC_HTTP_CLIENT", http_client)

    sg.call("/echo", json=json, data=data)

    assert http_client.execute.call_args.kwargs["content"] == expected


def test_scaling_group_tracing_sets_otel_sampler_env() -> None:
    sg = ScalingGroup(
        image="nginx:alpine", name="traced", env={"APP": "1"}, tracing=True
    )
    assert sg._env == {
        "APP": "1",
        "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
        "OTEL_TRACES_SAMPLER_ARG": "1.0",
    }


def test_scaling_group_tracing_off_and_default() -> None:
    assert ScalingGroup(image="nginx:alpine", name="off", tracing=False)._env == {
        "OTEL_TRACES_SAMPLER": "always_off"
    }
    assert ScalingGroup(image="nginx:alpine", name="default")._env == {}


@pytest.mark.parametrize(
    "target", [None, ExecutionBackend.ENVIRONMENT, ExecutionBackend.SERVERLESS]
)
def test_scaling_group_deploy_forwards_execution_target(
    target: ExecutionBackend | None,
) -> None:
    client = ScalingGroupClient()
    group = ScalingGroup(image="nginx:alpine", execution_target=target)
    group._client = cast(Any, client)
    group.deploy()

    request = client.requests[0]
    assert isinstance(request, sg_pb.CreateScalingGroupRequest)
    assert request.spec is not None and request.spec.container_spec is not None
    spec = request.spec.container_spec
    assert spec.has_field("execution_target") == (target is not None)
    if target is not None:
        assert (
            spec.execution_target.backend == container_pb.ExecutionBackend[target.name]
        )


def _response(status: int, body: bytes) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.content = body
    return response


_SCALING_UP = (
    b'{"error": "Service Unavailable", "message": "This scaling group is scaling up'
    b' from zero. Please retry in a moment.", "status": 503}'
)
_CONNECT_ERROR = (
    b"upstream connect error or disconnect/reset before headers."
    b" reset reason: connection timeout"
)


def _deployed_group(monkeypatch: pytest.MonkeyPatch, *responses: MagicMock) -> Any:
    client = ScalingGroupClient()
    sg = ScalingGroup(image="nginx:alpine", name="cold-start")
    sg._client = cast(Any, client)
    sg.deploy()
    assert sg._scaling_group_info is not None
    sg._scaling_group_info.web_url = "https://example.com"
    http_client = MagicMock()
    http_client.execute.side_effect = list(responses)
    monkeypatch.setattr(scaling_group_mod, "SYNC_HTTP_CLIENT", http_client)
    monkeypatch.setattr(scaling_group_mod.time, "sleep", lambda _: None)
    return sg, http_client


def test_scaling_group_deploy_waits_for_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_ready = _response(404, b"<html>not found</html>")
    health = _response(200, b"ok")
    http_client = MagicMock()
    http_client.execute.side_effect = [
        _response(404, b""),
        _response(404, b""),
        route_ready,
        health,
    ]
    monkeypatch.setattr(scaling_group_mod, "SYNC_HTTP_CLIENT", http_client)
    monkeypatch.setattr(scaling_group_mod.time, "sleep", lambda _: None)
    sg = ScalingGroup(image="nginx:alpine", name="route-ready")
    sg._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    sg.deploy()

    assert http_client.execute.call_count == 3
    assert sg.call("/health", method="GET") is health
    assert http_client.execute.call_count == 4
    assert http_client.execute.call_args.args[:2] == (
        "GET",
        "https://example.com/health",
    )


def test_scaling_group_deploy_route_wait_fails_open(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now = 0.0

    def sleep(delay: float) -> None:
        nonlocal now
        now += delay

    http_client = MagicMock()
    http_client.execute.return_value = _response(404, b"")
    monkeypatch.setattr(scaling_group_mod, "SYNC_HTTP_CLIENT", http_client)
    monkeypatch.setattr(scaling_group_mod, "_ROUTE_READY_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(scaling_group_mod.time, "monotonic", lambda: now)
    monkeypatch.setattr(scaling_group_mod.time, "sleep", sleep)
    sg = ScalingGroup(image="nginx:alpine", name="route-timeout")
    sg._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    with caplog.at_level("WARNING", logger=scaling_group_mod.__name__):
        assert sg.deploy() is sg

    assert "route-timeout" in caplog.text
    assert "404" in caplog.text


def test_scaling_group_deploy_route_wait_retries_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_client = MagicMock()
    http_client.execute.side_effect = [TimeoutError(), _response(200, b"ok")]
    monkeypatch.setattr(scaling_group_mod, "SYNC_HTTP_CLIENT", http_client)
    monkeypatch.setattr(scaling_group_mod.time, "sleep", lambda _: None)
    sg = ScalingGroup(image="nginx:alpine", name="route-connection-error")
    sg._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    assert sg.deploy() is sg

    assert http_client.execute.call_count == 2


def test_scaling_group_deploy_skips_route_for_zero_replicas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_client = MagicMock()
    monkeypatch.setattr(scaling_group_mod, "SYNC_HTTP_CLIENT", http_client)
    sg = ScalingGroup(image="nginx:alpine", name="scaled-to-zero", min_replicas=0)
    sg._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    sg.deploy()

    http_client.execute.assert_not_called()


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (404, b"", True),
        (404, b"<html>not found</html>", False),
        (200, b"ok", False),
        (500, b"error", False),
        (401, b"unauthorized", False),
    ],
)
def test_is_route_not_ready(status: int, body: bytes, expected: bool) -> None:
    assert scaling_group_mod._is_route_not_ready(_response(status, body)) is expected


@pytest.mark.parametrize("body", [_SCALING_UP, _CONNECT_ERROR, b"no healthy upstream"])
def test_scaling_group_call_retries_gateway_unavailable(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    ok = _response(200, b"ok")
    sg, http_client = _deployed_group(
        monkeypatch, _response(503, body), _response(503, body), ok
    )

    assert sg.call("/", json={"x": 1}) is ok
    assert http_client.execute.call_count == 3


def test_scaling_group_call_does_not_retry_app_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_unavailable = _response(503, b"maintenance")
    sg, http_client = _deployed_group(monkeypatch, app_unavailable)

    assert sg.call("/") is app_unavailable
    assert http_client.execute.call_count == 1


def test_scaling_group_call_cold_start_timeout_zero_disables_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unavailable = _response(503, _SCALING_UP)
    sg, http_client = _deployed_group(monkeypatch, unavailable)

    assert sg.call("/", cold_start_timeout=0) is unavailable
    assert http_client.execute.call_count == 1


def test_scaling_group_call_stops_retrying_when_group_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter(range(0, 1000, 20))
    monkeypatch.setattr(scaling_group_mod.time, "monotonic", lambda: next(clock))
    unavailable = _response(503, _SCALING_UP)
    sg, http_client = _deployed_group(monkeypatch, unavailable, unavailable)
    refresh = MagicMock(return_value=MagicMock(status="Unhealthy"))
    monkeypatch.setattr(sg, "refresh", refresh)

    assert sg.call("/") is unavailable
    assert http_client.execute.call_count == 1
    refresh.assert_called_once()
