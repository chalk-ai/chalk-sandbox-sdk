from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import chalkcompute._scaling_group as scaling_group_mod
from chalkcompute import ScalingGroup
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from tests._fakes import FakeMethod, StubbedConnectClient


class _CapturingClient(StubbedConnectClient):
    def __init__(self, *, authentication: str | None = None) -> None:
        self.requests: list[Any] = []
        self._access_token = "test-token"
        self.ensure_auth_calls = 0
        self.authentication = authentication

    def _ensure_auth(self) -> None:
        self.ensure_auth_calls += 1

    def call(
        self,
        method: FakeMethod,
        request: Any,
        **_: Any,
    ) -> Any:
        self.requests.append(request)
        if method.name == "create_scaling_group":
            assert isinstance(request, sg_pb.CreateScalingGroupRequest)
            assert request.spec is not None and request.spec.container_spec is not None
            spec = request.spec
            name = spec.container_spec.name
            response_type = sg_pb.CreateScalingGroupResponse
        elif method.name == "get_scaling_group":
            assert isinstance(request, sg_pb.GetScalingGroupRequest)
            name = request.name or "existing-group"
            spec = sg_pb.ScalingGroupSpec(
                container_spec=container_pb.ChalkContainerSpec(
                    authentication=self.authentication
                )
            )
            response_type = sg_pb.GetScalingGroupResponse
        else:
            raise AssertionError(f"unexpected method: {method.name}")
        return response_type(
            scaling_group=sg_pb.ScalingGroupResponse(
                id="sg-1",
                name=name,
                status="Available",
                web_url="https://example.com",
                ready_replicas=1,
                available_replicas=1,
                spec=spec,
            )
        )


@pytest.mark.parametrize(
    ("authenticated", "expected"),
    [(False, None), (True, "AUTHENTICATED")],
)
def test_scaling_group_deploy_serializes_authentication(
    authenticated: bool, expected: str | None
) -> None:
    client = _CapturingClient()
    sg = ScalingGroup(
        image="nginx:alpine",
        name="auth-test",
        authenticated=authenticated,
    )
    sg._client = cast(Any, client)

    sg.deploy()

    request = client.requests[0]
    assert isinstance(request, sg_pb.CreateScalingGroupRequest)
    assert request.spec is not None and request.spec.container_spec is not None
    assert (request.spec.container_spec.authentication or None) == expected


def test_scaling_group_deploy_forwards_tags_and_chalk_identity() -> None:
    client = _CapturingClient()
    sg = ScalingGroup(
        image="nginx:alpine",
        name="tagged-group",
        tags={"team": "compute"},
        chalk_identity=True,
    )
    sg._client = cast(Any, client)

    sg.deploy()

    request = client.requests[0]
    assert isinstance(request, sg_pb.CreateScalingGroupRequest)
    assert request.spec is not None and request.spec.container_spec is not None
    assert dict(request.spec.container_spec.tags) == {"team": "compute"}
    assert request.spec.container_spec.has_field("chalk_workload_identity")


def test_authenticated_scaling_group_call_adds_chalk_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _CapturingClient()
    sg = ScalingGroup(
        image="nginx:alpine",
        name="auth-test",
        authenticated=True,
    )
    sg._client = cast(Any, client)
    sg.deploy()
    original_headers = {"X-Test": "value"}
    http_client = MagicMock()
    monkeypatch.setattr(scaling_group_mod, "SYNC_HTTP_CLIENT", http_client)

    sg.call("/health", method="GET", headers=original_headers)

    assert http_client.execute.call_args.kwargs["headers"] == {
        "X-Test": "value",
        "Authorization": "Bearer test-token",
    }
    assert original_headers == {"X-Test": "value"}
    assert client.ensure_auth_calls == 1


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
    client = _CapturingClient()
    sg = ScalingGroup(image="nginx:alpine", name="content-test")
    sg._client = cast(Any, client)
    sg.deploy()
    http_client = MagicMock()
    monkeypatch.setattr(scaling_group_mod, "SYNC_HTTP_CLIENT", http_client)

    sg.call("/echo", json=json, data=data)

    assert http_client.execute.call_args.kwargs["content"] == expected


def test_scaling_group_call_preserves_explicit_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _CapturingClient()
    sg = ScalingGroup(
        image="nginx:alpine",
        name="auth-test",
        authenticated=True,
    )
    sg._client = cast(Any, client)
    sg.deploy()
    http_client = MagicMock()
    monkeypatch.setattr(scaling_group_mod, "SYNC_HTTP_CLIENT", http_client)

    sg.call(headers={"authorization": "Bearer caller-token"})

    assert http_client.execute.call_args.kwargs["headers"] == {
        "authorization": "Bearer caller-token"
    }
    assert client.ensure_auth_calls == 0


def test_from_name_recovers_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _CapturingClient(authentication="AUTHENTICATED")
    monkeypatch.setattr(
        scaling_group_mod,
        "ConnectClient",
        lambda chalk_client=None: client,
    )

    sg = ScalingGroup.from_name("existing-group")

    assert sg._authenticated is True


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
