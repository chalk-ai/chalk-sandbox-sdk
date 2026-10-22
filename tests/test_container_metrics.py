from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from e2etests.container_metrics import (
    ContainerMetricsTarget,
    _container_metrics_error,
    assert_container_metrics,
    assert_container_metrics_target,
)


@dataclass
class FakeInfo:
    name: str = "test-container"
    pod_name: str = "chalk-container-test-container"


class FakeContainer:
    id = "container-id"
    info = FakeInfo()


def target() -> ContainerMetricsTarget:
    return ContainerMetricsTarget(
        "test-container",
        "container-id",
        FakeInfo().pod_name,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def snapshot(observed: datetime | None = None) -> dict[str, Any]:
    return {
        "metricsBackend": "CHART_METRICS_BACKEND_VICTORIA_METRICS",
        "charts": [
            {
                "xSeries": [(observed or datetime.now(timezone.utc)).isoformat()],
                "series": [
                    {
                        "label": label,
                        "unit": unit,
                        "points": [{"value": value}],
                        "groupTags": [
                            {
                                "groupKey": "pod",
                                "value": {"utf8Value": FakeInfo().pod_name},
                            }
                        ],
                    }
                    for label, unit, value in [
                        ("Container CPU", "% cpu", 0),
                        ("Container Memory", "bytes", 4096),
                    ]
                ],
            }
        ],
    }


def test_assert_container_metrics_skips_when_not_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHALK_ASSERT_CONTAINER_METRICS", raising=False)
    monkeypatch.setattr(
        "subprocess.run", lambda *args, **kwargs: pytest.fail("unexpected CLI call")
    )
    assert_container_metrics(FakeContainer())  # type: ignore[arg-type]


def test_assert_container_metrics_requests_typed_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update(cmd=cmd, **kwargs)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(snapshot()), stderr=""
        )

    monkeypatch.setenv("CHALK_ASSERT_CONTAINER_METRICS", "1")
    monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-id")
    monkeypatch.delenv("CHALK_ACTIVE_ENVIRONMENT", raising=False)
    monkeypatch.setattr("subprocess.run", fake_run)
    assert_container_metrics(FakeContainer())  # type: ignore[arg-type]
    assert captured["cmd"][:3] == ["chalk", "metrics", "snapshot"]
    assert captured["env"]["CHALK_ACTIVE_ENVIRONMENT"] == "env-id"
    assert 0 < captured["timeout"] <= 120
    payload = json.loads(captured["cmd"][-1])
    assert "metricsBackend" not in payload
    assert {s["metric"] for s in payload["metricConfig"]["series"]} == {
        "METRIC_KIND_POD_CONTAINER_CPU_UTILIZATION",
        "METRIC_KIND_POD_CONTAINER_MEMORY_BYTES",
    }
    for series in payload["metricConfig"]["series"]:
        assert "raw" not in series
        assert series["groupBy"] == ["GROUP_BY_KIND_POD_NAME"]
        assert series["filters"] == [
            {
                "kind": "FILTER_KIND_POD_NAME",
                "comparator": "COMPARATOR_KIND_EQ",
                "value": [FakeInfo().pod_name],
            }
        ]


def test_container_metrics_accepts_idle_and_multicore_cpu() -> None:
    response = snapshot()
    assert _container_metrics_error(response, target()) is None
    response["charts"][0]["series"][0]["points"][0]["value"] = 250
    assert _container_metrics_error(response, target()) is None


@pytest.mark.parametrize(
    "failure",
    [
        "missing_cpu",
        "missing_memory",
        "wrong_pod",
        "stale",
        "future",
        "null",
        "nan",
        "infinity",
        "negative",
        "wrong_units",
        "empty",
        "unequal_axis",
        "missing_identity",
    ],
)
def test_container_metrics_rejects_invalid_response(failure: str) -> None:
    response = copy.deepcopy(snapshot())
    chart = response["charts"][0]
    cpu, memory = chart["series"]
    if failure == "missing_cpu":
        chart["series"] = [memory]
    elif failure == "missing_memory":
        chart["series"] = [cpu]
    elif failure == "wrong_pod":
        cpu["groupTags"][0]["value"]["utf8Value"] = "other-pod"
    elif failure == "stale":
        chart["xSeries"] = ["2025-12-31T23:59:59Z"]
    elif failure == "future":
        chart["xSeries"] = [
            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        ]
    elif failure == "null":
        cpu["points"] = [{}]
    elif failure == "nan":
        cpu["points"][0]["value"] = float("nan")
    elif failure == "infinity":
        cpu["points"][0]["value"] = float("inf")
    elif failure == "negative":
        cpu["points"][0]["value"] = -1
    elif failure == "wrong_units":
        memory["unit"] = "MiB"
    elif failure == "empty":
        response["charts"] = []
    elif failure == "unequal_axis":
        cpu["points"] = []
    elif failure == "missing_identity":
        cpu["groupTags"] = []
    assert _container_metrics_error(response, target()) is not None


def test_assert_container_metrics_retries_both_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incomplete = snapshot()
    incomplete["charts"][0]["series"] = incomplete["charts"][0]["series"][:1]
    responses = iter([incomplete, snapshot()])
    sleeps: list[float] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(next(responses)), stderr=""
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("time.sleep", sleeps.append)
    assert_container_metrics_target(target(), timeout_secs=30, poll_interval_secs=0.25)
    assert sleeps == [0.25]


def test_assert_container_metrics_failure_mentions_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(AssertionError) as exc:
        assert_container_metrics_target(target(), timeout_secs=0, poll_interval_secs=0)
    assert "test-container" in str(exc.value)
    assert "container-id" in str(exc.value)
    assert FakeInfo().pod_name in str(exc.value)


@pytest.mark.parametrize("poll_interval", [1.0, 10.0])
def test_deadline_preserves_last_completed_metrics_diagnosis(
    monkeypatch: pytest.MonkeyPatch, poll_interval: float
) -> None:
    clock = [0.0]
    calls = []
    incomplete = snapshot()
    incomplete["charts"][0]["series"] = incomplete["charts"][0]["series"][:1]

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs["timeout"])
        if len(calls) > 1:
            clock[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(incomplete))

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr("time.sleep", sleep)
    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(
        AssertionError, match="missing fresh pod-scoped metrics.*Container Memory"
    ):
        assert_container_metrics_target(
            target(), timeout_secs=2, poll_interval_secs=poll_interval
        )
    assert calls == ([2.0, 1.0] if poll_interval == 1 else [2.0])


def test_zero_budget_does_not_start_metrics_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "subprocess.run", lambda *args, **kwargs: pytest.fail("deadline has expired")
    )
    with pytest.raises(AssertionError, match="no metrics snapshot completed"):
        assert_container_metrics_target(target(), timeout_secs=0, poll_interval_secs=1)
