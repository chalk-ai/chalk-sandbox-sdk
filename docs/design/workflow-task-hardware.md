# Chalk Workflows SDK: before / after (CHA-12147, draft)

Example: a workflow with a light `fetch` task and a heavy `train` task that needs a GPU image.

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
worker = cc.deploy_workflow_worker(cpu="4", memory="16Gi")   # no gpu option

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

@cc.task(retries=2, timeout="10m")                   # no image/hardware → runs on the workflow's worker
def fetch(day: str) -> list[int]: ...

@cc.task(timeout="1h", image=gpu, cpu="4", memory="16Gi", gpu="nvidia-l4")   # own scaling group + queue
def train(shard: int) -> str:
    import torch                                     # heavy imports inside the task body
    ...

@cc.workflow(image=base, cpu="1", memory="2Gi")      # sizes only the orchestration worker
async def nightly(day: str) -> list[str]:            # body unchanged
    shards = await fetch(day)
    return await asyncio.gather(*(train.submit(s) for s in shards))

# No deploy step: defining the workflow deploys it (at first call, or at script exit)

result = nightly.run(day="2026-11-03")               # blocks, returns the result
run    = nightly.defer(day="2026-11-03")             # returns a WorkflowRun handle immediately
run.result(); run.status(); run.cancel(); run.send_message("approve", {...})

cc.RemoteWorkflow.from_name("nightly").defer(day="2026-11-03")   # another process: name only

nightly.run_local(day="2026-11-03")                  # local code, real orchestrator (hardware ignored)
await nightly(day="2026-11-03")                      # plain call: in-process, for unit tests
```

## Differences

| | Before | After |
|---|---|---|
| Imports | `chalk.workflows` + `chalkcompute` (+ `ChalkClient`) | `chalkcompute` only |
| Dependencies | `# /// script` comment block | `image=` everywhere; the SDK adds the workflow runtime itself |
| Task options | `retries`, `timeout` | `retries`, `timeout`, `image`, `cpu`, `memory`, `gpu` |
| Workflow options | none (all sizing on the deploy call) | `image`, `cpu`, `memory`, `gpu` (no `retries`: workflows don't retry) |
| Hardware | One size for the whole file | Per task; tasks with hardware get their own group and queue |
| Deploy | `cc.deploy_workflow_worker(...)` | Implicit when the workflow is defined |
| Deployment unit | File (`wf-<stem>`) | Workflow name |
| Run and wait | `worker.trigger(wf, {...}, wait=True)` / `cc.run_workflow(wf, {...})` | `wf.run(**kwargs)` |
| Start, return immediately | `worker.trigger(wf, {...})` → data-only handle | `wf.defer(**kwargs)` → `WorkflowRun` with `.result()` / `.status()` / `.cancel()` / `.send_message()` |
| Call from elsewhere | `ChalkClient().trigger_workflow(name, ..., task_queue=...)` | `cc.RemoteWorkflow.from_name(name)` |
| Local code, real orchestrator | `ChalkClient().run_workflow(wf, {...})` | `wf.run_local(**kwargs)` |
| Input style | dict | keyword arguments |

## Rules users need to know (after)

1. Every worker imports the whole file, so heavy packages (`torch`) go in the task's `image=` and are imported *inside* the task body.
2. Tasks without `image`/`cpu`/`memory`/`gpu` run on the workflow's worker.
3. `run_local` ignores task hardware and runs everything in your process.

## Deferred (not in this draft)

- `max_replicas` and per-replica concurrency for task groups.
- Versioning, server-side `EnsureWorkflow`, backlog-based scaling (implementation, not usage).
- Fate of `deploy_workflow_worker` / `cc.run_workflow` / `cc.trigger_workflow`.
