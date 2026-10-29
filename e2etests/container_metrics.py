"""Helpers for asserting e2e containers have pod-scoped metrics."""

from __future__ import annotations

import json
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
        last_error = _check_container_metrics_target(target)
        if last_error is None:
            print(f"Found container metrics for podName={target.pod_name}")
            return

        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval_secs)

    raise AssertionError(
        "Timed out waiting for container metrics "
        f"(name={target.name} id={target.id} podName={target.pod_name}): {last_error}"
    )


def _check_container_metrics_target(target: ContainerMetricsTarget) -> str | None:
    try:
        output = _run_metrics_snapshot(target.pod_name, start=target.observed_at)
        if _has_metric_datapoint(json.loads(output)):
            return None
        return "metrics snapshot returned no datapoints"
    except (json.JSONDecodeError, subprocess.SubprocessError) as e:
        return str(e)


def _run_metrics_snapshot(pod_name: str, *, start: datetime | None = None) -> str:
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
                    "name": "Container CPU Utilization",
                    "windowFunction": "WINDOW_FUNCTION_KIND_MAX",
                    "groupBy": [],
                },
                {
                    "metric": "METRIC_KIND_POD_CONTAINER_MEMORY_BYTES",
                    "filters": [pod_name_filter],
                    "name": "Container Memory",
                    "windowFunction": "WINDOW_FUNCTION_KIND_MAX",
                    "groupBy": [],
                },
            ],
        },
        "startTime": start.isoformat().replace("+00:00", "Z"),
        "endTime": now.isoformat().replace("+00:00", "Z"),
        "useStartAsOrigin": True,
        "useSketchMetricsTable": False,
    }


def _has_metric_datapoint(value: Any) -> bool:
    """Return true if a metrics response contains any non-null point value."""
    if isinstance(value, dict):
        if "points" in value and isinstance(value["points"], list):
            for point in value["points"]:
                if isinstance(point, dict) and point.get("value") is not None:
                    return True
                if isinstance(point, (int, float)):
                    return True
        return any(_has_metric_datapoint(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_metric_datapoint(v) for v in value)
    return False
