#!/usr/bin/env -S uv run
"""Load benchmark for CHA-10029: N x (create -> poll -> exec -> stop) cycles.

Derived from the script Ari used to find the bug. That script had to
monkeypatch ``httpx.post`` process-wide with a pooled client to stop the run
from exhausting the local fd limit; this one does not, because
``chalkcompute._http`` now pools connections for the whole process. If this
file ever needs that hack back, the fix has regressed.

What it measures:

* **fd high-water mark** — the headline number. Before pooling this climbed
  with parallelism until the process hit ``EMFILE`` (``[Errno 24]``, plus
  ``getaddrinfo`` failures once resolution could no longer get a socket).
  Pooled and multiplexed over HTTP/2, it should stay roughly flat.
* per-phase latency (boot / exec / stop), which is dominated by handshakes
  when connections are not reused.
* the negotiated HTTP version and how many TCP connections carried the run.

Opt-in — it creates real containers against staging and is far too slow for
the default suite::

    CHALK_SANDBOX_RUN_POOL_BENCHMARK=1 pytest e2etests/test_connection_pool_benchmark.py

or standalone, matching the original script's interface::

    uv run e2etests/test_connection_pool_benchmark.py [count] [parallelism]
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest

from chalkcompute import ComputeClass, Container
from chalkcompute._connect import ConnectClient

try:  # pragma: no cover - exercised only when running the benchmark
    from e2etests.conftest import needs_staging
except ImportError:  # standalone invocation
    needs_staging = pytest.mark.skipif(False, reason="")

TICKET = "cha-10029"
BENCHMARK_ENV_VAR = "CHALK_SANDBOX_RUN_POOL_BENCHMARK"

CONTAINER_IMAGE = os.environ.get("CHALK_SANDBOX_SMOKE_IMAGE", "nginx")
COMPUTE_CLASS = ComputeClass[
    os.environ.get("CHALK_SANDBOX_SMOKE_COMPUTE_CLASS", "HOST").upper()
]

PHASE_STYLES = {"boot": "cyan", "exec": "green", "stop": "magenta"}
MAX_TIMELINE_ROWS = 40

needs_benchmark_opt_in = pytest.mark.skipif(
    os.environ.get(BENCHMARK_ENV_VAR, "") not in ("1", "true", "yes"),
    reason=f"{BENCHMARK_ENV_VAR} not set (benchmark creates real containers)",
)


# ---------------------------------------------------------------------------
# fd sampling
# ---------------------------------------------------------------------------


def open_fd_count() -> int:
    """Return this process's open file-descriptor count."""
    try:
        import psutil

        return psutil.Process().num_fds()
    except Exception:
        # /dev/fd on macOS and BSD, /proc/self/fd on Linux.
        for path in ("/dev/fd", "/proc/self/fd"):
            try:
                return len(os.listdir(path))
            except OSError:
                continue
        return -1


class FdSampler:
    """Poll the fd count on a background thread and keep the maximum.

    The peak is what matters: the failure mode is a burst of concurrent
    handshakes, which a before/after sample would miss entirely.
    """

    def __init__(self, interval: float = 0.05) -> None:
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.baseline = open_fd_count()
        self.high_water = self.baseline

    def _run(self) -> None:
        while not self._stop.is_set():
            self.high_water = max(self.high_water, open_fd_count())
            self._stop.wait(self._interval)

    def __enter__(self) -> FdSampler:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.high_water = max(self.high_water, open_fd_count())


# ---------------------------------------------------------------------------
# cycles
# ---------------------------------------------------------------------------


@dataclass
class Phase:
    name: str
    start: float  # offsets from run start, in seconds
    end: float


@dataclass
class CycleResult:
    n: int
    name: str
    phases: list[Phase] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def run_cycle(n: int, name: str, client: ConnectClient, base: float) -> CycleResult:
    result = CycleResult(n=n, name=name)

    @contextmanager
    def timed(phase_name: str):
        start = time.perf_counter() - base
        try:
            yield
        finally:
            result.phases.append(Phase(phase_name, start, time.perf_counter() - base))

    try:
        # `client=` is the supported injection point; the original script had
        # to assign the private `_client` attribute here.
        container = Container(
            image=CONTAINER_IMAGE,
            name=name,
            compute_class=COMPUTE_CLASS,
            client=client,
        )

        with timed("boot"):
            container.run()
        try:
            with timed("exec"):
                exec_result = container.exec("echo", "hello")
                if exec_result.exit_code != 0:
                    raise RuntimeError(
                        f"exec failed with exit code {exec_result.exit_code}: "
                        f"{exec_result.stderr_text}"
                    )
                if exec_result.stdout_text.strip() != "hello":
                    raise RuntimeError(f"unexpected stdout: {exec_result.stdout_text!r}")
        finally:
            with timed("stop"):
                container.stop()
    except Exception as exc:
        result.error = str(exc)
    return result


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, math.ceil(p / 100 * len(ordered)) - 1))
    return ordered[k]


def pool_diagnostics(api_server: str) -> dict[str, object]:
    """Report what the shared pool actually did — the point of the ticket."""
    from chalkcompute import _http

    try:
        pool = _http.get_client(api_server)._transport._pool  # type: ignore[attr-defined]
        connections = list(pool.connections)
    except Exception as exc:  # pragma: no cover - diagnostics only
        return {"error": str(exc)}

    return {
        "http2_enabled": _http.http2_enabled(),
        "pooling_enabled": _http.pooling_enabled(),
        "connections": len(connections),
        "detail": [str(c) for c in connections],
    }


def result_summary(
    results: list[CycleResult],
    elapsed: float,
    run_id: str,
    sampler: FdSampler,
    api_server: str,
) -> dict:
    ok = [r for r in results if r.ok]
    phase_seconds = {}
    for phase_name in PHASE_STYLES:
        durations = [
            ph.end - ph.start for r in ok for ph in r.phases if ph.name == phase_name
        ]
        if durations:
            phase_seconds[phase_name] = {
                "avg": round(sum(durations) / len(durations), 3),
                "p50": round(percentile(durations, 50), 3),
                "p95": round(percentile(durations, 95), 3),
                "max": round(max(durations), 3),
            }
    errors = Counter((r.error or "")[:160] for r in results if not r.ok)
    return {
        "run_id": run_id,
        "count": len(results),
        "succeeded": len(ok),
        "failed": len(results) - len(ok),
        "elapsed_seconds": round(elapsed, 3),
        "cycles_per_second": round(len(results) / elapsed, 3) if elapsed > 0 else 0,
        "fd_baseline": sampler.baseline,
        "fd_high_water": sampler.high_water,
        "fd_growth": sampler.high_water - sampler.baseline,
        "pool": pool_diagnostics(api_server),
        "phase_seconds": phase_seconds,
        "errors": dict(errors),
    }


def run_benchmark(count: int, parallelism: int, *, run_id: str | None = None) -> dict:
    """Drive ``count`` cycles at ``parallelism`` and return the summary dict."""
    logging.getLogger("chalkcompute").setLevel(logging.WARNING)
    run_id = run_id or f"{TICKET}-{uuid.uuid4().hex[:8]}"

    # One client for the run, so the token exchange happens once. Unlike the
    # original script this needs no pre-warming: _ensure_auth is now locked, so
    # the worker threads cannot race it.
    client = ConnectClient()
    client._ensure_auth()
    api_server = client._api_server or ""

    base = time.perf_counter()
    results: list[CycleResult] = []
    with FdSampler() as sampler:
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            futures = [
                pool.submit(run_cycle, n, f"{run_id}-{n}", client, base)
                for n in range(1, count + 1)
            ]
            for future in as_completed(futures):
                results.append(future.result())
        elapsed = time.perf_counter() - base

    return result_summary(results, elapsed, run_id, sampler, api_server)


# ---------------------------------------------------------------------------
# pytest entry point
# ---------------------------------------------------------------------------


@needs_staging
@needs_benchmark_opt_in
def test_connection_pool_holds_fds_flat() -> None:
    count = int(os.environ.get("CHALK_SANDBOX_BENCHMARK_COUNT", "20"))
    parallelism = int(os.environ.get("CHALK_SANDBOX_BENCHMARK_PARALLELISM", "10"))

    summary = run_benchmark(count, parallelism)
    print("RESULT_JSON " + json.dumps(summary), flush=True)

    assert summary["failed"] == 0, summary["errors"]

    pool = summary["pool"]
    # The whole run should ride a small number of connections rather than one
    # per RPC: ~4 RPCs per cycle means unpooled would be 4 * count.
    assert isinstance(pool, dict) and pool.get("connections", 0) <= parallelism

    # fd growth should track the connection count, not the request count.
    assert summary["fd_growth"] < 4 * count, (
        f"fd high-water grew by {summary['fd_growth']} over {count} cycles; "
        "connections are probably not being reused"
    )


# ---------------------------------------------------------------------------
# standalone
# ---------------------------------------------------------------------------


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    parallelism = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    if count < 1 or parallelism < 1:
        print(f"usage: {sys.argv[0]} [count] [parallelism]", file=sys.stderr)
        return 1

    print(
        f"==> {count} cycle(s), parallelism {parallelism} "
        f"(image={CONTAINER_IMAGE}, compute_class={COMPUTE_CLASS.name})"
    )
    summary = run_benchmark(count, parallelism)

    # Plain print so a wrapper can `grep '^RESULT_JSON '`.
    print("RESULT_JSON " + json.dumps(summary), flush=True)
    print(
        f"==> {summary['succeeded']}/{summary['count']} succeeded, "
        f"{summary['failed']} failed, e2e {summary['elapsed_seconds']}s, "
        f"fd {summary['fd_baseline']} -> {summary['fd_high_water']}, "
        f"pool {summary['pool']}"
    )
    if summary["failed"]:
        print("==> error summary:", file=sys.stderr)
        for message, n in summary["errors"].items():
            print(f"    [{n}x] {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
