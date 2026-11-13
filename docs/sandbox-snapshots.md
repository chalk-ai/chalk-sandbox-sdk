# Sandbox snapshots

The SDK exposes Chalk's sandbox snapshot API. A snapshot captures a running
HOST-compute sandbox and can be restored into one or more new sandboxes.
To save state and release the sandbox's compute resources, use `sandbox.suspend()`;
`sandbox.restore()` later resumes it with the same sandbox ID.

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

Pass `wait=False` to `snapshot()` to skip SDK completion polling, then use
`snapshot.wait(timeout=...)` before restoring. The initial capture request can
still take time. `snapshot.refresh()`
fetches its current status. `SandboxSnapshot.from_id(snapshot_id, client=client)`
loads a previously saved handle, and `client.list_snapshots(sandbox.id)` lists
snapshots created from that sandbox. Use `snapshot.delete()` or
`client.delete_snapshot(snapshot.id)` to delete one.

## Suspend and restore in place

Suspending a sandbox captures its state and releases its CPU, memory, and any GPUs
while keeping its identity. Calling `restore()` on that sandbox allocates compatible
compute resources and restores the saved state with the same sandbox ID:

For example, clone a repository, leave a note in its working tree, and suspend
the workspace until you are ready to continue:

```python
import chalkcompute

sandbox = chalkcompute.Sandbox(
    compute_class=chalkcompute.ComputeClass.HOST,
).run()
sandbox.exec("git", "clone", "--depth", "1", "https://github.com/psf/requests.git", "/workspace/requests")
sandbox.exec("sh", "-c", "echo 'Investigate the failing login test' > /workspace/requests/NOTES.md")
print(sandbox.id)  # Save the ID so you can reconnect later.
sandbox.suspend()  # Release the sandbox's compute resources.

# Later, restore the same sandbox and workspace.
sandbox.restore()
print(sandbox.refresh().status)
```

`sandbox.restore()` initiates restoration. Once `sandbox.refresh().status` is
`running`, inspect the working tree and continue without cloning it again:

```python
print(sandbox.exec("git", "-C", "/workspace/requests", "status", "--short").stdout_text)  # ?? NOTES.md
sandbox.terminate()
```

To reconnect from another process, use
`chalkcompute.Sandbox.from_id("sandbox-id")` with the saved ID.

This differs from `snapshot.restore()`, which creates a new sandbox from a saved
snapshot. `sandbox.restore()` brings a suspended sandbox back under its existing
identity.

Snapshot capture requires a HOST-compute sandbox. Pass
`compute_class=ComputeClass.HOST` when creating a sandbox you intend to snapshot.
For capture and restore boundaries, GPU support, and CLI commands, see
[Sandbox snapshots](https://docs.chalk.ai/docs/sandboxes/snapshots).
