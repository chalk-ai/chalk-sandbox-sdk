# Disposable weight sync validation

The standalone `ftqa_smoke.py` opts into one FTQA A100 80GB and real ChalkFS/vLLM
0.33 checks. It copies lightweight evidence locally, terminates the sandbox and
verifies its terminal status, then deletes and verifies removal of its unique
stream volume. These scripts do not run from imports or ordinary tests. The sandbox also has a one-hour maximum lifetime.

The source checkout must integrate both the publisher and serving feature branches.
This scripts PR deliberately adds no publisher implementation. For the recorded
8B run, the tested integration revision was `6caddc4f`; the exact Python source
archive and SHA256 are preserved alongside its evidence. Later runs should use
the latest integrated feature revisions rather than treating that pin as production.

```sh
PYTHONPATH=/path/to/integrated-sdk python scripts/weight_sync/ftqa_smoke.py \
  --source-root /path/to/integrated-sdk --env-file /path/to/ftqa.env \
  --model 8b --out /path/to/new-evidence-directory
```

Use an environment with the SDK, codec dependencies and valid FTQA credentials.
No model artifacts download to the workstation. The remote install preserves the
image's existing torch/vLLM versions through pip constraints. The script currently
runs as root and applies `chmod 1777 /dev/shm`, pending the independent hypervisor fix.

The check seeds a pinned HF checkpoint, loads native vLLM, changes one final norm
tensor densely, restores it, changes one norm element sparsely, verifies worker
tensor bytes, restores it, exercises a backward PIN and unpin, and compares the
sparse output with a fresh engine loaded from an independently materialized checkpoint.

Recorded Qwen3-8B/A100 result: all checks passed, including exact greedy output
equivalence for a sparse version. The sparse payload was **83 bytes**, changing
one of 8,190,735,360 model elements. Apply/pause was 104.18/104.24 ms for that
update and 15.35/15.40 ms for its restore. Publisher scans took 2.7–3.6 seconds
per publication. Backward PIN required a full 16.38 GB keyframe replay and paused
admissions for 8.892 seconds. These are correctness checks on a tiny changed
tensor, not evidence of representative model-wide 1% delta performance.

The recorded integration had an older `density` metric denominator (changed
tensor elements). Use `full_model_density` from its sparse phase for global
density. Current serving density counts transmitted elements across the whole
model; dense fallback is an upper bound on actual changed elements. Older
`publish_to_visible_s` evidence included byte auditing and metadata reads; new
runs stop this timer immediately after the first versioned inference response.

Always inspect `launcher.json` and require both `sandbox_released` and
`volume_deleted` to be true, even after failure. If the launcher is interrupted,
recover the exact recorded sandbox ID and stream name and release only those
owned resources. Do not infer GPU cleanup merely from remote driver completion.

## Real training and separate live serving

`training_smoke.py` runs a real `cc.training` submission with a Qwen3-8B BF16
model and an independent authenticated serving ScalingGroup. It performs two
full-parameter SGD steps on two pinned GSM8K training rows, checks every trainable
parameter has a gradient, publishes updates, and records concurrent deterministic
inference responses. This is an SFT integration smoke, not a GRPO or reward-quality
benchmark. Actual changed BF16 bits and transmitted elements are reported separately.

Prepare the CUDA/vLLM image on CPU first; this includes both `python` and `python3`
and imports the exact backend training entrypoint. Use an integrated SDK checkout
with the publisher, codec, serving, and training-volume feature branches:

```sh
PYTHONPATH=/path/to/integrated-sdk python scripts/weight_sync/training_smoke.py \
  --source-root /path/to/integrated-sdk --env-file /path/to/ftqa.env \
  --out /path/to/cpu-preparation

# This CPU-only command pauses after full proof and CPU compute release.
PYTHONPATH=/path/to/integrated-sdk python scripts/weight_sync/cpu_seed.py \
  --prepared-image /path/to/cpu-preparation/image.json \
  --env-file /path/to/ftqa.env --out /path/to/cpu-seed

# Start only after CPU proof passes. This waits for independent authorization.
PYTHONPATH=/path/to/integrated-sdk python scripts/weight_sync/training_smoke.py \
  --source-root /path/to/integrated-sdk --env-file /path/to/ftqa.env \
  --prepared-image /path/to/cpu-preparation/image.json \
  --cpu-seed-ledger /path/to/cpu-seed/launcher.json \
  --out /path/to/new-training-evidence --execute \
  --steps 2 --budget-seconds 2700 --serving-startup-seconds 600 \
  --training-timeout-seconds 1200 --external-ssh-host USER@KNOWN_OWNED_HOST
```

CPU preparation builds both the base image and actual packaged training source.
Run launchers from outside another SDK checkout (for example `/tmp`) so the
explicit `PYTHONPATH` selects the integrated package.

CPU seeding uses a fixed twenty-minute lease, K8S two CPUs and 32GiB memory,
with at most fifteen minutes for startup. Its detached guard and sleep inhibitor
are ready before one non-replayed CreateSandbox RPC. The payload runs once in a
NEVER-restarting container entrypoint. Only short read-only progress polls use
ExecCommand: a retried long K8S ExecCommand can rerun a successful publication.
An exclusive guest marker prevents re-entry; atomic proof/completion files
survive a lost polling response. All source shard bytes, immutable shard bytes,
CURRENT, keyframe manifest, tensor catalog, config and metadata are verified.
The CPU sandbox and retained UID-owned pods stop before the launcher pauses.
Only the input volume remains, under the original unextended lease.

The receiver reserves the CPU lease exclusively and arms its detached guard
before dataset or training metadata creation. It reuses the exact stream name,
image, source overlay and packaged trainer, without another HF download.
`gpu-handoff-ready.json` publishes its unique phase, deadline and canonical
execution-config SHA. An independent reviewer supplies `cpu-authorization.json`
in the CPU evidence directory only after reviewing this ready proof and the
CPU proof. The authorization binds the CPU phase, proof SHA, volume and immutable
snapshot, HF revision, image/overlay/trainer identities, GPU phase/deadline,
execution-config SHA, and explicit transferred-volume cleanup ownership.
The launchers never self-authorize. A duplicate or stale receiver claim is refused.

CPU and receiver guards overlap data ownership during transfer. The receiver
requires a confirmed CPU watchdog exit and unchanged immutable CURRENT/manifest
before its first GPU request. Data-cleanup errors keep the receiver guard armed
for independent retries. The forty-five-minute example budget starts before
receiver metadata preparation and is never extended; it uses at most one live
serving A100 and one trainer A100. Both the initial and fresh serving starts are
bounded to ten minutes. The trainer is claimed only with at least 1,920 seconds
remaining (twenty-minute training cap, fresh-start reserve and cleanup margin).
The final full state is reconstructed and compared byte-for-byte with every
canonical trained tensor. The trainer and live group stop before a fresh group
loads that independent checkpoint for exact greedy-output comparison.

Before training, the launcher closes an actual SSE completion after its first
chunk. Twenty readiness samples, a healthy follow-up completion, zero failure
metrics, and identical SG owner/replica UIDs and native API/worker PID start times
are required. The external SSH check executes only a fixed process identity probe
inside the exact owned replica. Failure stops the run and releases resources.

The launcher records all resource IDs immediately. A detached `resource_watchdog.py`
shares the immutable deadline, survives launcher death, inhibits workstation idle
sleep, and recovers ambiguous submissions only by the unique test name. It cancels
the run, stops its container, verifies the exact `cc-<run UUID>` CR and UID-owned pods
are absent, and deletes only its owned CR using a UID precondition if retained after
API termination. It never removes finalizers. ScalingGroup cleanup checks the exact
CR and replicas bearing its immutable owner UID. Data deletion follows verified
compute release. Dataset absence is checked through the active-list query because
GetDataset can still return tombstoned rows. Inspect `launcher.json` and detached
watchdog evidence after every execution; an API terminal status alone is insufficient.

The launcher discovers actual serving placement across existing staging serverless
Kubernetes contexts instead of assuming GetEnv's default job cluster. Read access
to these contexts is required for cleanup proof. `placement_watchdog.py` is an
additional recovery tool for an older launcher that omitted the real context; it
preserves an independent append-only group-owner ledger and verifies cleanup even
when the API already reports the group absent.

`publish_to_first_response_s` is an observation across different hosts with
unmeasured clock uncertainty. Engine apply and admission pause metrics use local
monotonic durations. A single observer can skip intermediate published versions;
its report lists those omissions. Source archives, manifests, worker audits, exact
revision pins, and cleanup reports provide the evidence needed to interpret a run.

For repeated guest progress checks, `guest_progress.py` requires the launcher's
ledger, exact group and replica names, Kubernetes context, and known external SSH
host. It verifies the live replica's group owner UID before running a fixed
read-only probe. It reports seed/cache bytes, process stage, and published keyframe
sizes without reading credentials or downloading model data to the workstation.

Future executions cap OMP/MKL and Torch intra-op CPU threads to the explicit
`--cpu-threads` budget (default four) before model loading. A HOST guest can see
many more host CPUs than its quota; an earlier advancing run exposed hundreds of
library threads during CPU-to-GPU model transfer. Record the chosen budget with
training evidence, and retain the exact executed source when comparing runs.

After cleanup, `fetch_logs.py` retrieves bounded retained application or access
logs through protobuf gRPC using the backend repository's authoritative schema.
It requires `grpcio`, `protobuf`, `protoc`, and the SDK authentication dependencies.
Pass the actual cluster ID from `CloudComponentsService.ListServerlessClusters`;
a Kubernetes cluster slug is not an API cluster ID. For example:

```bash
python scripts/weight_sync/fetch_logs.py \
  --env-file /path/to/ftqa.env --proto-root /path/to/chalk-private/protos/protos \
  --cluster-id ACTUAL_CLUSTER_ID --start 2026-11-05T06:59:30Z \
  --end 2026-11-05T07:01:30Z --query 'scaling_group:EXACT_OWNED_GROUP_ID' \
  --out /path/to/evidence/serving-logs.json
```

Each page contains at most 500 application logs; the default limit is four pages.
Use `--access` for access logs. The optional `--scaling-group-id` access scope may
fail after deletion because the API checks the current group row; a query filter
can still search retained cluster access logs. Credentials never enter the output.

## Reusing the CPU guard

`cpu_seed_guest.py` is pure standard library code. A payload sets a `proof` dict;
`run_seed_once` records its result durably and `read_progress` only reads files.
For another bounded CPU preflight, reuse these helpers from `cpu_seed.py`:

- `arm_cpu_watchdog(record, ledger)` starts and verifies the independent guard.
- `guarded_sandbox_client(record, ledger)` scopes no-replay behavior to creation.
- `make_guest_program(phase, code, config)` builds the entrypoint and read-only poll.
- `poll_guest_until_complete(sandbox, record, ledger, poll_code)` retains evidence.
- `read_guest_file(..., max_bytes=..., record=record)` exports at most 20MiB through
  short read-only chunks, without requiring HOST filesystem sessions.
- `release_cpu(record, out)` stops the sandbox and verifies exact CR/UID pods.

Use the Team.GetEnv-derived namespace/context fields shown in the launcher and
preflight every actual cleanup context. Set `owned_volume_names=[]` for a preflight
that creates no volume. After successful compute and data release, record status
`released`, write the matching phase disarm, wait for the watchdog to exit and
check its exit proof. An ambiguous create remains watchdog-covered through its
RPC deadline plus reconciliation grace.

`cpu_seed_status.py --ledger ... --verify-volume-absence --env-file ...` is an
independent read-only release check. It checks retained owner UIDs even after the
CR disappears and verifies the authenticated environment before volume queries.
An active seed holds its input volume intentionally; volume absence is required
after expiry/failure, rather than at the paused handoff gate.

CPU guard regressions make no resource claims:

```sh
python -m pytest -q -o pythonpath=/path/to/integrated-sdk \
  scripts/weight_sync/test_*.py
```
