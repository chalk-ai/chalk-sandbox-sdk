from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

import pytest

from e2etests.container_metrics import (
    _CONTAINER_METRICS_TARGETS,
    _has_metric_datapoint,
    assert_container_metrics,
    register_container_metrics,
)


@dataclass
class FakeInfo:
    name: str = "test-container"
    pod_name: str = "chalk-container-test-container"


class FakeContainer:
    id = "container-id"
    info = FakeInfo()


def test_assert_container_metrics_skips_when_not_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.delenv("CHALK_ASSERT_CONTAINER_METRICS", raising=False)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert_container_metrics(FakeContainer())  # type: ignore[arg-type]

    assert calls == []


def test_assert_container_metrics_builds_pod_filtered_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"charts": [{"series": [{"points": [{"value": 1.0}]}]}]}),
            stderr="",
        )

    monkeypatch.setenv("CHALK_ASSERT_CONTAINER_METRICS", "1")
    monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-id")
    monkeypatch.delenv("CHALK_ACTIVE_ENVIRONMENT", raising=False)
    monkeypatch.setattr("subprocess.run", fake_run)

    assert_container_metrics(FakeContainer())  # type: ignore[arg-type]

    cmd = captured["cmd"]
    assert cmd[:3] == ["chalk", "metrics", "snapshot"]
    assert "--json" in cmd
    assert "--no-spinners" in cmd
    assert "--skip-version-check" in cmd
    assert captured["env"]["CHALK_ACTIVE_ENVIRONMENT"] == "env-id"

    payload = json.loads(cmd[-1])
    assert payload["useSketchMetricsTable"] is False
    series = payload["metricConfig"]["series"]
    assert {s["metric"] for s in series} == {
        "METRIC_KIND_CONTAINER_COUNT",
        "METRIC_KIND_CONTAINER_MEMORY_BYTES",
    }
    for metric_series in series:
        assert metric_series["filters"] == [
            {
                "kind": "FILTER_KIND_POD_NAME",
                "comparator": "COMPARATOR_KIND_EQ",
                "value": ["chalk-container-test-container"],
            }
        ]


def test_assert_container_metrics_retries_until_datapoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            {"charts": [{"series": [{"points": [{"value": None}]}]}]},
            {"charts": [{"series": [{"points": [{"value": 5.0}]}]}]},
        ]
    )
    sleeps: list[float] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(next(responses)), stderr="")

    monkeypatch.setenv("CHALK_ASSERT_CONTAINER_METRICS", "1")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("time.sleep", sleeps.append)

    assert_container_metrics(
        FakeContainer(),  # type: ignore[arg-type]
        timeout_secs=30,
        poll_interval_secs=0.25,
    )

    assert sleeps == [0.25]


def test_register_container_metrics_defers_snapshot_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    monkeypatch.setenv("CHALK_ASSERT_CONTAINER_METRICS", "1")
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: calls.append((args, kwargs)))

    try:
        register_container_metrics(FakeContainer())  # type: ignore[arg-type]
    finally:
        _CONTAINER_METRICS_TARGETS.clear()

    assert calls == []


def test_assert_container_metrics_failure_mentions_container_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"charts": [{"series": [{"points": [{"value": None}]}]}]}),
            stderr="",
        )

    monkeypatch.setenv("CHALK_ASSERT_CONTAINER_METRICS", "1")
    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(AssertionError) as exc:
        assert_container_metrics(
            FakeContainer(),  # type: ignore[arg-type]
            timeout_secs=0,
            poll_interval_secs=0,
        )

    message = str(exc.value)
    assert "test-container" in message
    assert "container-id" in message
    assert "chalk-container-test-container" in message


def test_has_metric_datapoint_accepts_nested_point_values() -> None:
    assert _has_metric_datapoint({"charts": [{"series": [{"points": [{"value": 0}]}]}]})
    assert not _has_metric_datapoint(
        {"charts": [{"series": [{"points": [{"value": None}]}]}]}
    )
