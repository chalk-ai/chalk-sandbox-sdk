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

result = nightly.run(day="2026-11-03")               # blocks, returns the result
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
| Run and wait | `worker.trigger(wf, {...}, wait=True)` / `cc.run_workflow(wf, {...})` | `wf.run(**kwargs)` |
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
| Queue | `chalkcompute-workflows.<workflow>.<version>.<hash(spec)>` (full names; Temporal allows 1000 chars) | `chalkcompute-workflows.nightly.3f9a1c2e.b71d04aa` |
| Scaling group | `<prefix>-<version[:8]>-<hash(spec)[:8]>` | `nightly-3f9a1c2e-b71d04aa` |

Scaling group names must be a single DNS label: at most 63 characters, lowercase letters, digits and dashes. Every name fits by construction, whatever the workflow's name:

- **The hashes carry the identity.** Two 8-character hashes plus their dashes take 18 characters, leaving at most 45 for `<prefix>`.
- **`<prefix>` is only for humans.** It is the workflow name lowercased, with `_` and `.` replaced by `-`, other characters dropped, and truncated to 45 characters. If the result is empty or starts with a digit, it becomes `wf-` plus what's left.
- **Truncation can't cause collisions.** The version hash covers the *full* workflow name, so two workflows whose names truncate to the same prefix still get different group names.
- **Full names are stored with the group** (workflow name, version and spec), so the UI shows them instead of the truncated label.
- **The server computes the names.** If workflow workers ever expose a port, their public hostname `<environment id>-<group name>` has a smaller budget that depends on the environment id, which only the server knows. Workers serve no port today, so 63 applies.

For comparison, `RemoteFunction` truncates long names to 63 characters, so two long function names with the same first 63 characters map to the same scaling group.

For the example above, one deploy creates:

| Spec | Runs | Group |
|---|---|---|
| `base`, 1 CPU, 2Gi | `nightly`, `fetch` | `nightly-<ver>-<h1>` |
| `gpu`, 4 CPU, 16Gi, `nvidia-l4` | `train`, `evaluate` | `nightly-<ver>-<h2>` |

Because the version is in every name, a run stays on the groups and queues of the version it started on, even if the workflow is redeployed mid-run.

## Deployment

**How functions do it.** `@cc.function` starts a background deploy as soon as the decorator runs. That works because a function's deploy depends only on that function.

**Why workflows can't start that early.** A workflow's deploy depends on every task it uses: their specs decide the scaling groups and feed the version hash. When `@cc.workflow` runs, tasks defined below it, or in modules imported later, don't exist yet.

**So the decorators only register, and the deploy starts at the first of:**

| Trigger | Behavior |
|---|---|
| `wf.deploy()` | Deploys now and blocks until done; raises on failure |
| First `wf.run()` / `wf.defer()` in the process | Deploys if needed, then starts the run |
| Interpreter exit | Deploys workflows that were defined but never deployed or called, so a script that only defines workflows still deploys. A failure exits the process with a non-zero status |

By then every task the workflow uses has been defined, so the deploy sees the complete set.

**How a call knows whether to deploy.** Each workflow object tracks one state per process:

```
PENDING ──first trigger──► DEPLOYING (one shared in-flight deploy) ──► DEPLOYED(version) | FAILED(error)
```

- `PENDING`: `.run()` / `.defer()` start the deploy and wait for it.
- `DEPLOYING`: they wait on the deploy already in flight. Concurrent callers share one deploy.
- `DEPLOYED`: they start the run directly, with no deploy and no extra RPC.

The first deploy in a process always asks the server (`EnsureWorkflow`), because a new process can't know what's already deployed. The server compares content hashes, and an unchanged workflow comes back immediately as reused: no image builds, no new groups, no new version. `RemoteFunction` behaves the same through `EnsureExternalFunction`.

**No duplicate deploys.** The exit hook only deploys workflows still in `PENDING`, and `.run()` / `.defer()` skip workflows already `DEPLOYED`:

| Script does | Ensure calls |
|---|---|
| Defines `nightly`, nothing else | 1, at exit |
| `nightly.deploy()` | 1 |
| `nightly.deploy()`, then `nightly.run()` ×3 | 1 |
| `nightly.run()` ×3, no `.deploy()` | 1, on the first call |
| `nightly.deploy()` twice | 2; the second comes back as reused |

An explicit `.deploy()` always asks the server again, as `RemoteFunction.deploy()` does. Even then, the server reuses identical content, so the worst case is one extra round trip, never a second set of groups or a new version.

**What `.deploy()` waits for:** images built, every scaling group accepted, the version recorded, and new runs pointed at it. It does not wait for running pods: workflow groups scale to zero, so there may be none.

**Errors** surface as they do for functions: raised from `.deploy()`, `.run()` or `.defer()`, or a non-zero exit when the exit-time deploy fails.

**Never deploy:** `cc.RemoteWorkflow.from_name(...)`, `wf.run_from_source(...)`, a plain `await wf(...)`, and every import inside a worker. An env var set in worker containers makes the decorators inert, as `CHALK_REMOTE_FUNCTION` does for functions.

**Startup latency.** Because the deploy starts later than a function's, the first `.run()` waits for it. To win most of that time back, each `@cc.task(image=...)` starts building its image in the background when the decorator runs. Images depend only on their own definition, so they're usually built by the time the deploy starts.

**Notebooks.** Re-running a cell that defines a workflow creates a new object in `PENDING`, so its next `.run()` deploys the edited code.

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

- `max_replicas` and per-replica concurrency for task groups.
- Implementation: how versions are computed, a server-side `EnsureWorkflow` RPC, backlog-based scaling.
- What happens to `deploy_workflow_worker`, `cc.run_workflow` and `cc.trigger_workflow`.
