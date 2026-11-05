# Weight workload CPU diagnostics

Run the stdlib script inside the affected Linux workload without changing its
source or pool settings:

```sh
python diagnose_weight_workload.py --pid 1 --sample-seconds 1 > thread-diagnostics.json
```

The script reports thread names/counts/states, sampled CPU ticks, logical CPUs,
affinity count, readable ancestor CPU quotas, memory limits/current bytes, and
throttling counters. It never reads command lines or environments. Copying and
running it claims no compute and changes no resource settings. Run it in the
same cgroup namespace as the target; if `--pid` points into another namespace,
provide the matching `--cgroup-root`. Unreadable or unlimited limits are null.
Only readable ancestors are visible; hidden parent quotas can be tighter.

Compare the largest thread-name groups with `minimum_visible_quota_cpus` and CPU
tick deltas before attributing latency. Idle threads alone do not prove CPU
contention; rising throttling counters and CPU-active groups are stronger evidence.
Linux truncates names, so multiple pools can share a group. An earlier snapshot
and a later snapshot can reveal which initialization adds a pool. The script
never imports PyTorch or native clients, avoiding new pool creation while measuring.

At the SDK's pinned native sources, VolumeClient uses one shared Tokio runtime
per process and explicitly chooses `available_parallelism().max(2)` workers.
`TOKIO_WORKER_THREADS` cannot override this explicit worker count. The blocking
RemoteCallClient creates a default Tokio runtime per client; its default worker
count can use `TOKIO_WORKER_THREADS`. Upload file reads use Tokio's blocking pool,
which is distinct from worker threads. Pool limits from OMP/MKL affect their own
libraries and do not cap these Tokio pools. Source inspection alone cannot tell
which pools account for a running process's thread count: preserve thread-name
measurements and the installed SDK/native version with experiment artifacts.

The associated script tests cover quota ancestry, fractional/unlimited limits,
thread names with spaces, CPU tick extraction, and omission of credential-bearing
command/environment data. This workflow does not require GPU allocation or a
native rebuild.
