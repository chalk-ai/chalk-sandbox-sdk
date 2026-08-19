from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

import chalkcompute._scaling_group as scaling_group_mod
from chalkcompute import ScalingGroup
from tests._typed_connect import TypedConnectFakeMixin


class _Response:
    status_code = 200
    text = ""

    def __init__(self, scaling_group: dict[str, Any]) -> None:
        self._scaling_group = scaling_group

    def json(self) -> dict[str, Any]:
        return {"scalingGroup": self._scaling_group}


class _CapturingClient(TypedConnectFakeMixin):
    def __init__(self, *, authentication: str | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self._access_token = "test-token"
        self.ensure_auth_calls = 0
        self.authentication = authentication

    def _ensure_auth(self) -> None:
        self.ensure_auth_calls += 1

    def rpc(
        self,
        _service: str,
        method: str,
        request: dict[str, Any],
        **_: Any,
    ) -> _Response:
        self.requests.append(request)
        if method == "CreateScalingGroup":
            spec = request["spec"]
            name = spec["containerSpec"]["name"]
        elif method == "GetScalingGroup":
            name = request.get("name", "existing-group")
            spec = {
                "containerSpec": {
                    "authentication": self.authentication,
                }
            }
        else:
            raise AssertionError(f"unexpected method: {method}")
        return _Response(
            {
                "id": "sg-1",
                "name": name,
                "status": "Available",
                "webUrl": "https://example.com",
                "readyReplicas": 1,
                "availableReplicas": 1,
                "spec": spec,
            }
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

    container_spec = client.requests[0]["spec"]["containerSpec"]
    assert container_spec.get("authentication") == expected


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
    captured: dict[str, Any] = {}

    def request(method: str, url: str, **kwargs: Any) -> object:
        captured.update(method=method, url=url, **kwargs)
        return object()

    monkeypatch.setattr(httpx, "request", request)

    sg.call("/health", method="GET", headers=original_headers)

    assert captured["headers"] == {
        "X-Test": "value",
        "Authorization": "Bearer test-token",
    }
    assert original_headers == {"X-Test": "value"}
    assert client.ensure_auth_calls == 1


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
    captured: dict[str, Any] = {}

    def request(_method: str, _url: str, **kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(httpx, "request", request)

    sg.call(headers={"authorization": "Bearer caller-token"})

    assert captured["headers"] == {"authorization": "Bearer caller-token"}
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
