"""Helpers for asserting e2e containers have pod-scoped metrics."""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from chalkcompute import Container


@dataclass(frozen=True)
class ContainerMetricsTarget:
    name: str
    id: str
    pod_name: str
    observed_at: datetime


def assert_container_metrics(
    container: Container,
    *,
    timeout_secs: float = 120.0,
    poll_interval_secs: float = 5.0,
) -> None:
    """Assert a started container has pod-scoped metrics in Chalk.

    The assertion is opt-in so local e2e runs do not require a Chalk CLI.
    CI enables it with ``CHALK_ASSERT_CONTAINER_METRICS=1``.
    """
    target = container_metrics_target(container)
    container_id = target.id
    container_name = target.name
    pod_name = target.pod_name

    print(
        "E2E container metrics target: "
        f"name={container_name} id={container_id} podName={pod_name or '<missing>'}"
    )

    if os.environ.get("CHALK_ASSERT_CONTAINER_METRICS") != "1":
        print(
            "Skipping container metrics assertion; CHALK_ASSERT_CONTAINER_METRICS != 1"
        )
        return

    if not pod_name:
        raise AssertionError(
            "Cannot assert container metrics without podName "
            f"(container name={container_name} id={container_id})"
        )

    assert_container_metrics_target(
        target,
        timeout_secs=timeout_secs,
        poll_interval_secs=poll_interval_secs,
    )


def container_metrics_target(container: Container) -> ContainerMetricsTarget:
    info = container.info
    return ContainerMetricsTarget(
        name=info.name if info is not None else "<missing>",
        id=container.id or "<missing>",
        pod_name=info.pod_name if info is not None else "",
        observed_at=datetime.now(timezone.utc),
    )


def assert_container_metrics_target(
    target: ContainerMetricsTarget,
    *,
    timeout_secs: float,
    poll_interval_secs: float,
) -> None:
    """Assert pod-scoped metrics for a previously captured container target."""
    deadline = time.monotonic() + timeout_secs
    last_error = ""
    while True:
        remaining = max(0.001, deadline - time.monotonic())
        last_error = _check_container_metrics_target(target, remaining)
        if last_error is None:
            print(f"Found container metrics for podName={target.pod_name}")
            return

        if time.monotonic() >= deadline:
            break
        time.sleep(min(poll_interval_secs, max(0, deadline - time.monotonic())))

    raise AssertionError(
        "Timed out waiting for container metrics "
        f"(name={target.name} id={target.id} podName={target.pod_name}): {last_error}"
    )


def _check_container_metrics_target(
    target: ContainerMetricsTarget, timeout_secs: float
) -> str | None:
    try:
        output = _run_metrics_snapshot(
            target.pod_name, start=target.observed_at, timeout_secs=timeout_secs
        )
        return _container_metrics_error(json.loads(output), target)
    except (json.JSONDecodeError, subprocess.SubprocessError, OSError) as error:
        return str(error)


def _run_metrics_snapshot(
    pod_name: str, *, start: datetime, timeout_secs: float
) -> str:
    payload = _metrics_snapshot_payload(pod_name, start=start)
    env = os.environ.copy()
    if not env.get("CHALK_ACTIVE_ENVIRONMENT") and env.get("CHALK_ENVIRONMENT_ID"):
        env["CHALK_ACTIVE_ENVIRONMENT"] = env["CHALK_ENVIRONMENT_ID"]

    result = subprocess.run(
        [
            "chalk",
            "metrics",
            "snapshot",
            "--json",
            "--no-spinners",
            "--skip-version-check",
            json.dumps(payload, separators=(",", ":")),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout_secs,
    )
    return result.stdout


def _metrics_snapshot_payload(
    pod_name: str,
    *,
    start: datetime | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    start = start or now
    start = min(start - timedelta(minutes=2), now - timedelta(minutes=10))
    pod_name_filter = {
        "kind": "FILTER_KIND_POD_NAME",
        "comparator": "COMPARATOR_KIND_EQ",
        "value": [pod_name],
    }
    return {
        "metricConfig": {
            "name": "Container E2E Metrics",
            "windowPeriod": "1m",
            "series": [
                {
                    "metric": "METRIC_KIND_POD_CONTAINER_CPU_UTILIZATION",
                    "filters": [pod_name_filter],
                    "name": "Container CPU",
                    "windowFunction": "WINDOW_FUNCTION_KIND_MAX",
                    "groupBy": ["GROUP_BY_KIND_POD_NAME"],
                },
                {
                    "metric": "METRIC_KIND_POD_CONTAINER_MEMORY_BYTES",
                    "filters": [pod_name_filter],
                    "name": "Container Memory",
                    "windowFunction": "WINDOW_FUNCTION_KIND_MAX",
                    "groupBy": ["GROUP_BY_KIND_POD_NAME"],
                },
            ],
        },
        "startTime": start.isoformat().replace("+00:00", "Z"),
        "endTime": now.isoformat().replace("+00:00", "Z"),
        "useStartAsOrigin": True,
        "useSketchMetricsTable": False,
    }


def _container_metrics_error(
    response: Any, target: ContainerMetricsTarget
) -> str | None:
    """Check the public dense-chart contract for both resources of the owned pod."""
    if not isinstance(response, dict):
        return "metrics snapshot is not a chart response"
    expected = {"Container CPU": "% cpu", "Container Memory": "bytes"}
    found: set[str] = set()
    for chart in response.get("charts", []):
        if not isinstance(chart, dict):
            continue
        timestamps = chart.get("xSeries", [])
        for series in chart.get("series", []):
            if not isinstance(series, dict):
                continue
            label = series.get("label")
            if label not in expected or series.get("unit") != expected[label]:
                continue
            tags = series.get("groupTags", [])
            if not any(
                isinstance(tag, dict)
                and tag.get("groupKey") == "pod"
                and tag.get("value", {}).get("utf8Value") == target.pod_name
                for tag in tags
            ):
                continue
            points = series.get("points", [])
            if len(points) != len(timestamps):
                continue
            for timestamp, point in zip(timestamps, points):
                try:
                    observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except (AttributeError, TypeError, ValueError):
                    continue
                value = point.get("value") if isinstance(point, dict) else None
                if (
                    observed.tzinfo is None
                    or observed < target.observed_at
                    or observed > datetime.now(timezone.utc)
                ):
                    continue
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                ):
                    continue
                # CPU is a percentage of allocation (or one core when unlimited).
                # An unlimited multi-core workload can legitimately exceed 100%.
                if value < 0 or (label == "Container Memory" and value == 0):
                    continue
                found.add(label)
                break
    missing = expected.keys() - found
    return (
        "missing fresh pod-scoped metrics with expected units: "
        + ", ".join(sorted(missing))
        if missing
        else None
    )
