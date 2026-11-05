# Disposable weight sync validation

These opt-in scripts claim one FTQA A100 80GB, run real ChalkFS/vLLM 0.33 checks,
copy lightweight evidence locally, terminate the sandbox and verify its terminal
status, then delete and verify removal of the unique stream volume. They do not
run from imports or ordinary tests. The sandbox also has a one-hour maximum lifetime.

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

PYTHONPATH=/path/to/integrated-sdk python scripts/weight_sync/training_smoke.py \
  --source-root /path/to/integrated-sdk --env-file /path/to/ftqa.env \
  --prepared-image /path/to/cpu-preparation/image.json \
  --out /path/to/new-training-evidence --execute --seed-on-serving \
  --steps 2 --budget-seconds 2400
```

The second invocation builds the packaged training source before claiming GPUs.
Its default GPU budget is forty minutes, starting before the first serving claim.
It uses at most one serving and one training A100 80GB simultaneously. Seeding on
the planned serving GPU avoids a separate acquisition worker. The final full state
is independently reconstructed and compared byte-for-byte with every canonical
trained tensor. The trainer and live group stop before a fresh group loads that
independent checkpoint for exact greedy-output comparison.

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
