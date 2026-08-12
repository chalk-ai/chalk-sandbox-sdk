from __future__ import annotations

from typing import Any, cast

from chalkcompute import CronScalingTrigger, CronScalingWindow, ScalingGroup


class _Response:
    status_code = 200
    text = ""

    def json(self) -> dict[str, Any]:
        return {
            "scalingGroup": {
                "id": "sg-1",
                "name": "sg-cron",
                "status": "Running",
                "readyReplicas": 1,
                "availableReplicas": 1,
            }
        }


class _CapturingClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def rpc(
        self,
        _service: str,
        method: str,
        request: dict[str, Any],
        **_: Any,
    ) -> _Response:
        assert method == "CreateScalingGroup"
        self.requests.append(request)
        return _Response()


def _deployed_scaling_spec(sg: ScalingGroup) -> dict[str, Any]:
    client = _CapturingClient()
    sg._client = cast(Any, client)
    sg.deploy()
    return client.requests[0]["spec"]["scalingSpec"]


def test_deploy_includes_cron_scaling_trigger() -> None:
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-cron",
        port=8080,
        min_replicas=0,
        max_replicas=8,
        cron_scaling_trigger=CronScalingTrigger(
            timezone="America/New_York",
            windows=[
                CronScalingWindow(
                    start="0 8 * * 1-5", end="0 18 * * 1-5", desired_replicas=3
                )
            ],
        ),
    )

    assert _deployed_scaling_spec(sg)["cronScalingTrigger"] == {
        "timezone": "America/New_York",
        "windows": [
            {"start": "0 8 * * 1-5", "end": "0 18 * * 1-5", "desiredReplicas": 3}
        ],
    }


def test_deploy_omits_cron_scaling_trigger_by_default() -> None:
    sg = ScalingGroup(image="ubuntu:latest", name="sg-cron", port=8080)

    assert "cronScalingTrigger" not in _deployed_scaling_spec(sg)


def test_cron_scaling_trigger_with_multiple_windows() -> None:
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-cron",
        port=8080,
        min_replicas=0,
        max_replicas=8,
        cron_scaling_trigger=CronScalingTrigger(
            timezone="UTC",
            windows=[
                CronScalingWindow(
                    start="0 8 * * *", end="0 12 * * *", desired_replicas=2
                ),
                CronScalingWindow(
                    start="0 13 * * *", end="0 18 * * *", desired_replicas=4
                ),
            ],
        ),
    )

    scaling_spec = _deployed_scaling_spec(sg)
    assert scaling_spec["cronScalingTrigger"]["timezone"] == "UTC"
    assert len(scaling_spec["cronScalingTrigger"]["windows"]) == 2
