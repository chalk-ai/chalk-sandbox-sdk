"""End-to-end tests for HOST sandbox snapshots and suspend/restore on FTQA."""

from __future__ import annotations

import time
import uuid

from chalkcompute import ComputeClass, Sandbox, SandboxClient, SandboxSnapshot
from e2etests.conftest import needs_staging


def _assert_file(sandbox: Sandbox, expected: str) -> None:
    result = sandbox.exec("cat", "/tmp/chalk-snapshot-e2e.txt", timeout_secs=60)
    assert result.exit_code == 0, f"cat failed: {result.stderr_text}"
    assert result.stdout_text.strip() == expected


def _wait_for_status(
    sandbox: Sandbox,
    statuses: set[str],
    *,
    timeout: float = 180,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = sandbox.refresh()
        if info.status in statuses:
            return
        if info.status in {"failed", "error", "terminated"}:
            raise AssertionError(
                f"Sandbox {sandbox.id} entered {info.status}: {info.status_message}"
            )
        time.sleep(2)
    raise TimeoutError(
        f"Sandbox {sandbox.id} did not reach {sorted(statuses)} in {timeout}s"
    )


@needs_staging
def test_sandbox_snapshot_fork_suspend_restore() -> None:
    """Snapshots fork state; restoring a suspended sandbox keeps its ID."""
    client = SandboxClient.from_env()
    sandboxes: list[Sandbox] = []
    snapshot: SandboxSnapshot | None = None
    snapshot_deleted = False
    try:
        source = client.create(
            image="ubuntu:latest",
            name=f"snapshot-e2e-{uuid.uuid4().hex[:8]}",
            lifetime="900s",
            compute_class=ComputeClass.HOST,
            timeout=300,
        )
        sandboxes.append(source)
        original_id = source.id
        assert original_id is not None

        marker = f"snapshot-state-{uuid.uuid4().hex}"
        write = source.exec(
            "sh",
            "-c",
            f"printf '%s' '{marker}' > /tmp/chalk-snapshot-e2e.txt",
            timeout_secs=60,
        )
        assert write.exit_code == 0, f"Could not write state: {write.stderr_text}"

        snapshot = source.snapshot(timeout=300)
        assert snapshot.status == "completed"
        loaded = client.get_snapshot(snapshot.id)
        assert loaded.status == "completed"
        assert loaded.source_sandbox_id == source.id
        assert any(item.id == snapshot.id for item in client.list_snapshots(source.id))

        restored = snapshot.restore(timeout=300)
        fork = snapshot.restore(timeout=300)
        sandboxes.extend((restored, fork))
        assert restored.id is not None and fork.id is not None
        assert len({source.id, restored.id, fork.id}) == 3
        _assert_file(restored, marker)
        _assert_file(fork, marker)

        snapshot.delete()
        snapshot_deleted = True
        assert all(item.id != snapshot.id for item in client.list_snapshots(source.id))

        source.suspend()
        _wait_for_status(source, {"suspended"})
        source.restore()
        _wait_for_status(source, {"running", "succeeded"})
        assert source.id == original_id
        _assert_file(source, marker)
    finally:
        if snapshot is not None and not snapshot_deleted:
            try:
                snapshot.delete()
            except Exception as exc:
                print(f"warning: could not delete test snapshot: {type(exc).__name__}")
        for sandbox in reversed(sandboxes):
            try:
                sandbox.terminate()
            except Exception as exc:
                print(
                    f"warning: could not terminate test sandbox: {type(exc).__name__}"
                )
        client.close()
