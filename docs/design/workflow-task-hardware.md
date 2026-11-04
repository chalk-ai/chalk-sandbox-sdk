# Chalk Workflows SDK: before / after (CHA-12147, draft)

Example: a workflow with a light task (`fetch`) and heavy tasks that need a GPU image (`train`, `evaluate`).

## Before (today)

```python
# /// script
# dependencies = ["chalkpy[workflows]", "chalkcompute", "torch"]
# ///
import asyncio
import chalkcompute as cc
from chalk.client import ChalkClient
from chalk.workflows import task, workflow           # decorators come from chalkpy

@task(retries=2, timeout="10m")
def fetch(day: str) -> list[int]: ...

@task(timeout="1h")                                  # no per-task cpu/memory/gpu/image
def train(shard: int) -> str: ...

@workflow
async def nightly(day: str) -> list[str]:
    shards = await fetch(day)
    return await asyncio.gather(*(train.submit(s) for s in shards))

# 1. Deploy: one scaling group for the whole file, sized for the heaviest task
worker = cc.deploy_workflow_worker(cpu="4", memory="16Gi")             # no gpu option

# 2. Run: input is a dict
result = worker.trigger(nightly, {"day": "2026-11-03"}, wait=True)     # blocks, returns the result
handle = worker.trigger(nightly, {"day": "2026-11-03"})                # WorkflowRunHandle: data only

result = cc.run_workflow(nightly, {"day": "2026-11-03"})               # shortcut for 1 + 2

# From another process: the queue name must be known
ChalkClient().trigger_workflow("nightly", {"day": "2026-11-03"},
                               task_queue="chalkcompute-workflows.wf-pipeline")

# Local code against the real orchestrator
ChalkClient().run_workflow(nightly, {"day": "2026-11-03"})
```

## After (proposed)

```python
import asyncio
import chalkcompute as cc

base = cc.Image.debian_slim()
gpu  = cc.Image.debian_slim().pip_install(["torch"])

@cc.task(retries=2, timeout="10m")                   # no specs → inherits the workflow's → workflow's group
def fetch(day: str) -> list[int]: ...

@cc.task(timeout="1h", image=gpu, cpu="4", memory="16Gi", gpu="nvidia-l4")
def train(shard: int) -> str:
    import torch                                     # heavy imports inside the task body
    ...

@cc.task(timeout="30m", image=gpu, cpu="4", memory="16Gi", gpu="nvidia-l4")   # same specs as train → same group
def evaluate(model: str) -> float: ...

@cc.workflow(image=base, cpu="1", memory="2Gi")
async def nightly(day: str) -> list[float]:          # body unchanged
    shards = await fetch(day)
    models = await asyncio.gather(*(train.submit(s) for s in shards))
    return await asyncio.gather(*(evaluate.submit(m) for m in models))

nightly.deploy()                                     # optional: deploy now and wait, like RemoteFunction.deploy()

result = nightly.remote(day="2026-11-03")            # blocks, returns the result
run    = nightly.defer(day="2026-11-03")             # returns a WorkflowRun handle immediately
run.result(timeout=600); run.status(); run.send_message("approve", {...})
run.cancel()                                         # planned, see below

cc.RemoteWorkflow.from_name("nightly").defer(day="2026-11-03")   # another process: name only

nightly.run_from_source(day="2026-11-03")            # local code, real orchestrator (task specs ignored)
await nightly(day="2026-11-03")                      # plain call: in-process, no orchestrator (unit tests)
```

## Differences

| | Before | After |
|---|---|---|
| Imports | `chalk.workflows` + `chalkcompute` (+ `ChalkClient`) | `chalkcompute` only |
| Dependencies | `# /// script` comment block | `image=` everywhere; the SDK adds the workflow runtime itself |
| Task options | `retries`, `timeout` | `retries`, `timeout`, `image`, `cpu`, `memory`, `gpu` |
| Workflow options | none (all sizing on the deploy call) | `image`, `cpu`, `memory`, `gpu` (no `retries`: workflows don't retry) |
| Hardware | One size for the whole file | Per spec: each distinct set of specs in a workflow gets its own scaling group and queue |
| Deploy | `cc.deploy_workflow_worker(...)` | Implicit, or `wf.deploy()` explicitly (like `RemoteFunction.deploy()`) |
| Deployment unit | File (`wf-<stem>`) | Workflow name |
| Run and wait | `worker.trigger(wf, {...}, wait=True)` / `cc.run_workflow(wf, {...})` | `wf.remote(**kwargs)` |
| Start, return immediately | `worker.trigger(wf, {...})` → data-only handle | `wf.defer(**kwargs)` → `WorkflowRun` handle (below) |
| Call from elsewhere | `ChalkClient().trigger_workflow(name, ..., task_queue=...)` | `cc.RemoteWorkflow.from_name(name)` |
| Local code, real orchestrator | `ChalkClient().run_workflow(wf, {...})` | `wf.run_from_source(**kwargs)` |
| Local code, no orchestrator | `await wf(...)` | `await wf(...)` (unchanged) |
| Input style | dict | keyword arguments |

## Scaling groups and queues

Each workflow version gets one scaling group and one queue per distinct spec, where spec = `(image, cpu, memory, gpu)`.

- Tasks with no specs inherit the workflow's, so they run on the workflow's own group.
- Tasks with identical specs share a group and queue; any difference means a separate group.
- `retries` and `timeout` are not part of the spec: they apply per call, not per worker.
- Specs are compared after normalization (`cpu=1` == `"1"` == `"1000m"`).
- Groups are scoped to one workflow: equal specs in two different workflows do not share a group.

| Name | Format | Example |
|---|---|---|
| Queue | `chalkcompute-workflows.<workflow>.<version>.<hash(spec)>` | `chalkcompute-workflows.nightly.3f9a1c2e.b71d04aa` |
| Scaling group | `<workflow>-<version[:8]>-<hash(spec)[:8]>` (a DNS label: no dots, ≤ 40 chars, workflow name truncated) | `nightly-3f9a1c2e-b71d04aa` |

For the example above, one deploy creates:

| Spec | Runs | Group |
|---|---|---|
| `base`, 1 CPU, 2Gi | `nightly`, `fetch` | `nightly-<ver>-<h1>` |
| `gpu`, 4 CPU, 16Gi, `nvidia-l4` | `train`, `evaluate` | `nightly-<ver>-<h2>` |

Because the version is in every name, a run stays on the groups and queues of the version it started on, even if the workflow is redeployed mid-run.

## `WorkflowRun` handle

| Method | Behavior |
|---|---|
| `.result(timeout=None)` | Blocks until the run finishes and returns its result; raises if the run failed or `timeout` passes |
| `.status()` | `RUNNING` while active; otherwise `COMPLETED`, `FAILED`, `CANCELED`, `TERMINATED` or `TIMED_OUT` |
| `.send_message(name, payload)` | Returns once the orchestrator has durably recorded the message (not once the workflow has handled it); raises if the run is not running |
| `.cancel()` | *Planned.* Requests cancellation; the workflow sees it at its next `await`, and tasks already running are not interrupted. Whether it goes through the API server (permission check, audit) is still open |

## Rules users need to know (after)

1. Every worker imports the whole file, so heavy packages (`torch`) go in the task's `image=` and are imported *inside* the task body.
2. Tasks without `image`/`cpu`/`memory`/`gpu` run on the workflow's group.
3. `run_from_source` ignores task specs and runs every task in your process.

## Open

- When an implicit deploy starts, what `.deploy()` waits for, and how deploy errors are reported.
- `max_replicas` and per-replica concurrency for task groups.
- Implementation: how versions are computed, a server-side `EnsureWorkflow` RPC, backlog-based scaling.
- What happens to `deploy_workflow_worker`, `cc.run_workflow` and `cc.trigger_workflow`.
