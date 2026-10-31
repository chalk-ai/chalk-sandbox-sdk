"""Redeploying a function must hand traffic to the new version without a gap.

Each deploy runs as its own process, the way a user re-runs the file that
defines their function. The scripts print one JSON line describing what callers
observed, and the tests assert on it:

* once ``deploy`` returns, every call is served by the new version, and none of
  them stalls while the old replica is retired;
* a new version that never becomes healthy is never routed to: the old version
  keeps answering and the deploy does not report success.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import _delete_function, needs_staging

# How long callers keep calling once a deploy has returned.
_OBSERVE_SECONDS = 20.0
# No single call after a redeploy may take longer than this. A healthy call is
# well under a second; the old-replica cutover stall measured 6.5-7.8s.
_MAX_CALL_SECONDS = 3.0
# How long the unhealthy-version test watches traffic while that version tries
# to come up. Long enough for its replica to be scheduled and started.
_UNHEALTHY_OBSERVE_SECONDS = 120.0

_PRELUDE = """\
import json
import os
import sys
import time

import chalkcompute

VERSION = {version!r}
NAME = {name!r}
"""

# In the pod the decorator is a no-op and CHALK_REMOTE_FUNCTION is set, so the
# import below hangs only there: the replica starts but never serves.
_HANG_IN_POD = """\
if os.environ.get("CHALK_REMOTE_FUNCTION"):
    time.sleep(3600)
"""

_FUNCTION = """\

@chalkcompute.function(name=NAME, compute_class=chalkcompute.ComputeClass.{compute_class})
def redeploy_cutover_probe(arg: str) -> str:
    return f"{VERSION}:{arg}"


def _call(fn) -> dict:
    start = time.monotonic()
    try:
        version = str(fn.remote("x")).split(":", 1)[0]
    except Exception as exc:
        version = f"error: {type(exc).__name__}: {exc}"[:300]
    return {"version": version, "seconds": time.monotonic() - start}


def _observe(fn, seconds: float) -> list[dict]:
    calls = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        calls.append(_call(fn))
    return calls
"""

_MAIN_HEALTHY = """\

if __name__ == "__main__":
    redeploy_cutover_probe.wait_ready(timeout=600)
    calls = _observe(redeploy_cutover_probe, {observe_seconds})
    print(json.dumps({{"calls": calls}}), flush=True)
"""

_MAIN_UNHEALTHY = """\

if __name__ == "__main__":
    # Call through a separate handle: the decorated one would wait for the
    # deploy this script started, which must never finish.
    calls = _observe(chalkcompute.RemoteFunction.from_name(NAME), {observe_seconds})
    try:
        redeploy_cutover_probe.wait_ready(timeout=1)
        deploy = "ready"
    except Exception as exc:
        deploy = f"{{type(exc).__name__}}: {{exc}}"[:300]
    print(json.dumps({{"calls": calls, "deploy": deploy}}), flush=True)
    # Skip waiting on the background deploy at interpreter exit.
    os._exit(0)
"""


def _write_script(
    directory: Path,
    *,
    name: str,
    version: str,
    compute_class: str,
    hang_in_pod: bool,
    main: str,
    observe_seconds: float,
) -> Path:
    source = _PRELUDE.format(version=version, name=name)
    if hang_in_pod:
        source += _HANG_IN_POD
    source += _FUNCTION.replace("{compute_class}", compute_class) + main.format(
        observe_seconds=observe_seconds
    )
    path = directory / f"redeploy_cutover_{version}.py"
    path.write_text(textwrap.dedent(source))
    return path


def _run(script: Path) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        cwd=script.parent,
        timeout=900,
    )
    if result.returncode != 0:
        pytest.fail(
            f"{script.name} failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout[-2000:]}\n"
            f"stderr: {result.stderr[-2000:]}"
        )
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    pytest.fail(f"{script.name} printed no result:\n{result.stdout[-2000:]}")


def _summary(calls: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for call in calls:
        counts[call["version"]] = counts.get(call["version"], 0) + 1
    slowest = max((call["seconds"] for call in calls), default=0.0)
    return f"{len(calls)} calls, versions={counts}, slowest={slowest:.2f}s"


# Kubernetes-backed and hypervisor-host-backed replicas roll out and route differently.
_COMPUTE_CLASSES = pytest.mark.parametrize("compute_class", ["K8S", "HOST"])


@needs_staging
@_COMPUTE_CLASSES
def test_redeploy_serves_new_version_as_soon_as_deploy_returns(
    tmp_path: Path, compute_class: str
) -> None:
    name = build_scoped_name(f"ex-redeploy-cutover-{compute_class.lower()}")
    try:
        for version in ("v1", "v2"):
            script = _write_script(
                tmp_path,
                name=name,
                version=version,
                compute_class=compute_class,
                hang_in_pod=False,
                main=_MAIN_HEALTHY,
                observe_seconds=_OBSERVE_SECONDS,
            )
            calls = _run(script)["calls"]
            summary = _summary(calls)
            assert calls, f"{version}: no calls completed"
            stale = [call for call in calls if call["version"] != version]
            assert not stale, (
                f"{version}: calls after deploy returned were not served by {version}: "
                f"{summary}; first={stale[0]}"
            )
            slow = [call for call in calls if call["seconds"] > _MAX_CALL_SECONDS]
            assert not slow, (
                f"{version}: calls stalled after deploy returned: {summary}; first={slow[0]}"
            )
    finally:
        _delete_function(name)


@needs_staging
@_COMPUTE_CLASSES
def test_unhealthy_new_version_keeps_old_version_serving(
    tmp_path: Path, compute_class: str
) -> None:
    name = build_scoped_name(f"ex-redeploy-unhealthy-{compute_class.lower()}")
    try:
        healthy = _write_script(
            tmp_path,
            name=name,
            version="v1",
            compute_class=compute_class,
            hang_in_pod=False,
            main=_MAIN_HEALTHY,
            observe_seconds=1.0,
        )
        assert {call["version"] for call in _run(healthy)["calls"]} == {"v1"}

        unhealthy = _write_script(
            tmp_path,
            name=name,
            version="v2",
            compute_class=compute_class,
            hang_in_pod=True,
            main=_MAIN_UNHEALTHY,
            observe_seconds=_UNHEALTHY_OBSERVE_SECONDS,
        )
        result = _run(unhealthy)
        calls = result["calls"]
        summary = _summary(calls)
        assert calls, "no calls completed while v2 was deploying"
        not_v1 = [call for call in calls if call["version"] != "v1"]
        assert not not_v1, (
            f"v1 stopped serving while unhealthy v2 deployed: {summary}; first={not_v1[0]}"
        )
        assert result["deploy"] != "ready", (
            f"deploy of a version that never serves reported ready: {summary}"
        )
    finally:
        _delete_function(name)
