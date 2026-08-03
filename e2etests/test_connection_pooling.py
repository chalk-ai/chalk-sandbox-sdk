"""Descriptor-usage regression test for CHA-10029.

The bug: ``ConnectClient.rpc()`` issued every RPC as a bare ``httpx.post``, so
descriptor use tracked *request count* without bound.  Under load the process
hit ``EMFILE`` -- ``[Errno 24] Too many open files``, and ``[Errno 8] nodename
nor servname provided`` once resolution could no longer get a socket either.

This drives read-only RPCs at high concurrency and asserts descriptor use stays
bounded by the pool's ceiling.  Read-only on purpose: it exercises the transport
without creating containers, so it is cheap enough to be worth running.

Opt-in, since it is a load test against staging::

    CHALK_SANDBOX_RUN_POOL_TEST=1 uv run pytest e2etests/test_connection_pooling.py -q -s

Set ``CHALK_SANDBOX_CONNECTION_POOLING=0`` alongside it to watch the original
failure reappear.
"""

from __future__ import annotations

import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from chalkcompute._connect import ConnectClient

from e2etests.conftest import needs_staging

_SERVICE = "chalk.container.v1.ContainerService"
_REQUESTS = int(os.environ.get("CHALK_SANDBOX_POOL_TEST_REQUESTS", "200"))
_PARALLELISM = int(os.environ.get("CHALK_SANDBOX_POOL_TEST_PARALLELISM", "100"))

needs_opt_in = pytest.mark.skipif(
    os.environ.get("CHALK_SANDBOX_RUN_POOL_TEST", "") not in ("1", "true", "yes"),
    reason="CHALK_SANDBOX_RUN_POOL_TEST not set (load test against staging)",
)


def open_fd_count() -> int:
    """Return this process's open file-descriptor count."""
    try:
        import psutil

        return psutil.Process().num_fds()
    except Exception:
        for path in ("/dev/fd", "/proc/self/fd"):  # macOS/BSD, then Linux
            try:
                return len(os.listdir(path))
            except OSError:
                continue
        return -1


class FdHighWater:
    """Track the peak descriptor count on a background thread.

    The peak is the whole point: the failure is a burst of concurrent
    handshakes, which sampling only before and after would miss.
    """

    def __init__(self, interval: float = 0.005) -> None:
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.baseline = open_fd_count()
        self.peak = self.baseline

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, open_fd_count())
            self._stop.wait(self._interval)

    def __enter__(self) -> FdHighWater:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.peak = max(self.peak, open_fd_count())


@needs_staging
@needs_opt_in
def test_descriptor_use_is_bounded_under_concurrency() -> None:
    from chalkcompute import _http

    client = ConnectClient()
    client._ensure_auth()

    outcomes: Counter[str] = Counter()

    def one_rpc(_n: int) -> str:
        try:
            status = client.rpc(_SERVICE, "ListContainers", {}).status_code
            return "ok" if status == 200 else f"HTTP {status}"
        except Exception as exc:
            return f"{type(exc).__name__}: {str(exc)[:60]}"

    with FdHighWater() as fds:
        with ThreadPoolExecutor(max_workers=_PARALLELISM) as pool:
            for outcome in pool.map(one_rpc, range(_REQUESTS)):
                outcomes[outcome] += 1

    connections = len(_http.get_client(client._api_server or "")._transport._pool.connections)
    print(
        f"\n{_REQUESTS} RPCs @ parallelism {_PARALLELISM}: "
        f"ok={outcomes['ok']}/{_REQUESTS} "
        f"fd {fds.baseline}->{fds.peak} (+{fds.peak - fds.baseline}) "
        f"connections={connections} "
        f"pooling={_http.pooling_enabled()}",
        flush=True,
    )

    assert outcomes["ok"] == _REQUESTS, dict(outcomes)

    # The ceiling, not the request count, is what bounds descriptors. Unpooled
    # this grew to one per in-flight request and hit EMFILE.
    assert connections <= _http._MAX_CONNECTIONS
    assert fds.peak - fds.baseline <= _http._MAX_CONNECTIONS + 16, (
        f"descriptor high-water grew by {fds.peak - fds.baseline} across "
        f"{_REQUESTS} requests; connections are not being reused"
    )
