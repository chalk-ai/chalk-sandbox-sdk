from __future__ import annotations

import pytest

from chalkcompute import VolumeError, VolumeNotFoundError
from e2etests.volume.benchmarks.cases.api_throughput import (
    bench_volumes,
    check_error_rate,
)
from e2etests.volume.benchmarks.harness import BenchmarkFailure


class FakeClient:
    def __init__(self, fail_every: int | None = None) -> None:
        self.fail_every = fail_every
        self.live: set[str] = set()
        self.creates = 0

    def create(self, name: str) -> None:
        self.creates += 1
        if self.fail_every and self.creates % self.fail_every == 0:
            # Some failed creates still land server side
            if self.creates % (2 * self.fail_every) == 0:
                self.live.add(name)
            raise VolumeError("boom")
        self.live.add(name)

    def delete_volume(self, name: str) -> None:
        if name not in self.live:
            raise VolumeNotFoundError("missing")
        self.live.remove(name)


def test_bench_volumes_creates_and_deletes_every_volume() -> None:
    client = FakeClient()
    create, delete = bench_volumes(client, 20, 4)  # type: ignore[arg-type]
    assert client.live == set()
    assert create["metadata"]["name"] == "create-volumes"
    assert delete["metadata"]["name"] == "delete-volumes"
    assert create["metadata"]["api_ops"] == delete["metadata"]["api_ops"] == 20
    assert create["metadata"]["api_error_rate"] == 0


def test_bench_volumes_counts_failures_and_cleans_up() -> None:
    client = FakeClient(fail_every=5)
    create, delete = bench_volumes(client, 20, 1)  # type: ignore[arg-type]
    assert client.live == set()
    assert create["metadata"]["api_errors"] == 4
    assert create["metadata"]["api_error_rate"] == 0.2
    assert delete["metadata"]["api_ops"] == 16
    assert delete["metadata"]["api_errors"] == 0


def test_check_error_rate_fails_above_threshold() -> None:
    def results(rate: float) -> dict:
        return {
            "benchmarks": [{"metadata": {"name": "read-files", "api_error_rate": rate}}]
        }

    check_error_rate(results(0.05), 0.05)
    with pytest.raises(BenchmarkFailure, match="read-files 5.1%"):
        check_error_rate(results(0.051), 0.05)


def test_first_attempt_failures_counts_real_sdk_retries(monkeypatch) -> None:
    from types import SimpleNamespace

    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    from chalkcompute import _rpc
    from e2etests.volume.benchmarks.cases.api_throughput import (
        count_first_attempt_failures,
    )

    monkeypatch.setattr(_rpc.time, "sleep", lambda _: None)
    ctx = SimpleNamespace(
        method=SimpleNamespace(
            service_name="chalk.volume.v2.VolumeService", name="CreateVolume"
        )
    )
    attempts = iter([True, True, False, True, False])

    def call_next(request, ctx):
        if next(attempts):
            raise ConnectError(Code.UNAVAILABLE, "unavailable")
        return "ok"

    with count_first_attempt_failures() as retried:
        # One request needs two retries and the next needs one
        assert (
            _rpc.RetryInterceptor().intercept_unary_sync(call_next, None, ctx) == "ok"
        )
        assert (
            _rpc.RetryInterceptor().intercept_unary_sync(call_next, None, ctx) == "ok"
        )
    assert retried.by_method == {"CreateVolume": 2}
