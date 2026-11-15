# Sandbox snapshots

The SDK exposes Chalk's sandbox snapshot API. A snapshot captures a running
HOST-compute sandbox and can be restored into one or more new sandboxes.

## Capture and restore

```python
from chalkcompute import ComputeClass, SandboxClient

with SandboxClient.from_env() as client:
    sandbox = client.create(compute_class=ComputeClass.HOST)
    restored = None
    fork = None
    try:
        sandbox.exec(
            "python",
            "-c",
            "from pathlib import Path; Path('/workspace/state.txt').write_text('ready')",
        )

        # Wait for capture to finish (the default).
        snapshot = sandbox.snapshot()

        # Every restore gets its own sandbox, so restoring twice forks the same state.
        restored = snapshot.restore()
        fork = snapshot.restore()

        result = restored.exec("cat", "/workspace/state.txt")
        assert result.stdout_text == "ready"
    finally:
        if fork is not None:
            fork.terminate()
        if restored is not None:
            restored.terminate()
        sandbox.terminate()
```

`sandbox.snapshot()` returns only after capture and durable publication have
completed, so the result is ready to restore. The SDK handles submission, retries,
and completion internally. Pass `timeout=...` to limit the total time spent in the
call (the default is 300 seconds).

A timeout stops the call without cancelling an accepted capture.
`SandboxSnapshotTimeoutError.snapshot_id` identifies that capture when submission
was acknowledged. Save the ID and restore it later:

```python
from chalkcompute import SandboxSnapshot, SandboxSnapshotTimeoutError

try:
    snapshot = sandbox.snapshot(timeout=60)
except SandboxSnapshotTimeoutError as exc:
    if exc.snapshot_id is None:
        # Submission was not confirmed. Inspect saved snapshots before trying again.
        raise
    saved_snapshot_id = exc.snapshot_id
    # Later, possibly in another process:
    snapshot = SandboxSnapshot.from_id(saved_snapshot_id, client=client)

# If capture is still completing, restore handles readiness internally.
restored = snapshot.restore()
```

Each `sandbox.snapshot()` call creates a new capture. To recover an accepted
capture, use its snapshot ID. `client.list_snapshots(sandbox.id)` lists saved
snapshots; `snapshot.delete()` removes a saved snapshot.

## Suspend and restore in place

Suspending a sandbox captures its state and releases its running resources while
keeping its identity. Calling `restore()` on that sandbox restores the saved state
in place and keeps the same sandbox ID:

```python
sandbox.suspend()
sandbox.restore()
```

This differs from `snapshot.restore()`, which creates a new sandbox from a saved
snapshot. `sandbox.restore()` brings a suspended sandbox back under its existing
identity.

Snapshot capture currently requires a HOST-compute sandbox. The CLI defaults
sandbox creation to HOST compute; the SDK preserves its existing compute-class
default, so pass `compute_class=ComputeClass.HOST` when creating a sandbox you
intend to snapshot. Snapshot retention and the precise set of runtime state
captured are determined by the service.
