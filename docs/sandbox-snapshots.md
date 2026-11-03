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

Pass `wait=False` to `snapshot()` to return while capture is still in progress,
then use `snapshot.wait(timeout=...)` before restoring. `snapshot.refresh()`
fetches its current status. `SandboxSnapshot.from_id(snapshot_id, client=client)`
loads a previously saved handle, and `client.list_snapshots(sandbox.id)` lists
snapshots created from that sandbox. Use `snapshot.delete()` or
`client.delete_snapshot(snapshot.id)` to delete one.

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
