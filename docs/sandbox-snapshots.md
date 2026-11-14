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

Pass `wait=False` to submit capture and return a pending handle immediately after
acceptance. Checkpointing, upload, and registry publication continue independently
of your connection. `snapshot.refresh()` reports `status`, `phase`,
`bytes_completed`, and `bytes_total` (byte counts are zero when unavailable).
Upload progress counts bytes processed by the upload pipeline, which may still be
buffered or in flight. Only `status == "completed"` proves durable upload and
registry publication. Use `snapshot.wait(timeout=...)` before restoring.

A wait timeout stops only the local wait. It raises
`SandboxSnapshotTimeoutError`, whose `snapshot` attribute retains the handle:

```python
from chalkcompute import SandboxSnapshotTimeoutError

snapshot = sandbox.snapshot(wait=False)
print(snapshot.id)  # Save this to reconnect from another process.
try:
    snapshot.wait(timeout=60)
except SandboxSnapshotTimeoutError as exc:
    snapshot = exc.snapshot
    # Capture continues. Wait again later, or reload the saved ID.
```

`SandboxSnapshot.from_id(snapshot_id, client=client)` reloads a saved handle, and
`client.list_snapshots(sandbox.id)` lists snapshots created from that sandbox.
Use `snapshot.delete()` or `client.delete_snapshot(snapshot.id)` to delete one.

For recovery when a submission response is lost, supply an `idempotency_key`:

```python
snapshot = sandbox.snapshot(wait=False, idempotency_key="model-ready-2026-11-13")
```

Keys must be non-empty and at most 128 UTF-8 bytes. Repeating that submission
with the same key returns the same operation. Reusing
the key with different inputs is rejected. If no key is supplied, the SDK creates
one; submission failures expose it as `SandboxSnapshotError.idempotency_key` so
you can retry with that key. Status reads and reconnecting never start a capture.
The updated SDK requires backend support for asynchronous submission; an older
server reports an unsupported-operation error rather than falling back to a
long-running synchronous request.

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
