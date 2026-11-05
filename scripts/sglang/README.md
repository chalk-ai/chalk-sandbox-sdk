# SGLang compatibility preflight

These tools prepare a CPU-only image/fixture proof for the experimental adapter.
They do not validate device behavior or enable the backend SGLang catalog.
`inspect_registry.py` reads public Docker Hub manifests and small OCI configs;
it never requests image layers and bounds every response to4MiB.

```bash
python3 scripts/sglang/inspect_registry.py lmsysorg/sglang v0.5.23-runtime \
  --out /tmp/sglang-image-metadata
```

The pinned CUDA candidate is
`lmsysorg/sglang@sha256:354700f4404ab0b8dcb493bc61d5d11e7c2a91f3a7babb4efe741cb75d4b51c0`.
Its provenance names SGLang commit297e54fd105e974cdf401e5b4d20253a22c16f90.
The proposed A100 host's driver595.71.05 passes the native CUDA13>=580 gate;
actual package/kernel execution still requires preflight and a separate GPU run.

`cpu_preflight.py` defaults to local preparation with no resource APIs. It freezes
the serving Python overlay, CPU payload, controller and reviewed lifecycle
helpers into a new output directory. Supply the existing `scripts/weight_sync`
guard directory and the saved native source files from the audited upstream
commit. The guard dependency is explicit: `cpu_seed.py`, `cpu_seed_guest.py`,
`training_smoke.py` and `ftqa_smoke.py`, reviewed at validation commitbacdc6da;
their exact bytes are recorded rather than imported from a mutable checkout
during execution. They provide one non-replayed create, detached deadline and
launcher-death cleanup, phase-bound readiness, immutable-UID CR teardown, and
bounded read-only status/file polling. The payload executes once in a
NEVER-restarting container entrypoint; polls cannot restart it.

```bash
python scripts/sglang/cpu_preflight.py --out /tmp/sglang-cpu-plan \
  --source-root /path/to/this/sdk \
  --guard-root /path/to/validated/sdk/scripts/weight_sync \
  --audited-source-root /path/to/audited/sglang-source-files
```

Review `plan.json` and frozen source before the explicit execution command:

```bash
python /tmp/sglang-cpu-plan/cpu_preflight.py --execute \
  --out /tmp/sglang-cpu-plan --env-file /path/to/ftqa.env
```

Execution claims only one K8S CPU sandbox,4CPU/16GiB, hard900second lease including
pull/startup and export, and no volumes. The detached guard must be live and
ready before the actual create. The image is passed as its raw digest string:
no image build, pip install, package resolver or model download occurs. No Chalk
credential environment is passed into the guest; `chalk_identity=False` also
disables the SDK default platform-managed creator identity. HF offline flags prevent implicit model
fetches. A second execution of the same plan fails its exclusive execution lock.

The payload records actual dependency versions, requires SGLang0.5.23,
Torch2.14.1/CUDA13 and Transformers5.19.0 matching the audited source pins, checks
six native control/loader/OpenAI source hashes, and never initializes CUDA.
Static AST checks verify disk reload/pause/resume fields and
`CompletionRequest.ignore_eos` without native imports; native CLI help and request
parsing remain explicitly unexecuted. A different
installed stack is a compatibility blocker for review; the script does not loosen
requirements. It generates a1,246,464parameter BF16 Llama/WordLevel-tokenizer
fixture with no HF download, keyframe plus two absolute patches, independent full
oracles and bitwise materialization checks. The v1 patch changes one output-head
row; v2 changes an attention row and doubles the final norm (covering sparse and
dense patches). Both consecutive CPU logit changes must exceed0.01 before device
testing with the fixed1e-3 fresh-reference tolerance. The <=20MiB bundle is exported with
short read-only32KiB reads (base64 responses remain below44KiB) and verified against
the durable guest proof. Each chunk must have its exact expected length; export
stops with120seconds reserved for cleanup. Progress records byte counts only.
Failures retain bounded, credential-redacted command error/exit/signal/stderr
diagnostics without the command, environment or exported data.

Finally, the launcher stops/verifies the exact CPU sandbox/container CR/pods,
deletes no external volumes, disarms the guard only after verified cleanup, and
retains small local proof/fixture/log files. On ambiguous create or failed cleanup,
the detached guard stays armed and reconciles the unique owned name. Preparation
and unit tests claim nothing. A successful CPU proof does not authorize a GPU
claim: submit a separate fixed-budget one-A100 plan after all prior resources are
released, with a fresh-reference output oracle and verified teardown.

## Optional CPU HOST prewarm

Use a separately reviewed `--host-prewarm` preparation when the exact image is
absent from the Oracle host cache. This still claims no GPU: the fixed plan uses
HOST/GVISOR,4CPU/16GiB, explicit `ExternalClusterHosts`, identity disabled and the
same900second lease. It reuses the offline CPU proof and bounded fixture export.

Before create, the guard requires a fresh (<60s) external-cluster pool report
containing only `svlsbfe05ebc-oracle-a100`, accepting placements with a connected
external control stream and eight NVIDIA A100 devices. The actual committed
placement must then match that host/pool. The API offers pool restriction rather
than exact host affinity; if membership or placement changes, the phase fails and
cleans up. No AMD/GPU demand or host scaling operation is submitted.

The frozen `host_guard.py` is also the detached watchdog entrypoint. It proves
read-only HOST inventory access before readiness, and wraps every normal,
deadline/death and ambiguous-create cleanup with exact runsc runtime and
containerd task absence checks on the Oracle host. It never stops unrelated
runtimes or shared daemons. Failed native verification leaves compute unreleased
and the guard armed. CR/retained-UID pods and native HOST absence are both required
before disarm; a wrong actual host requires its own native cleanup proof.

```bash
python scripts/sglang/cpu_preflight.py --host-prewarm --out /tmp/sglang-host-plan \
  --source-root /path/to/this/sdk \
  --guard-root /path/to/validated/sdk/scripts/weight_sync \
  --audited-source-root /path/to/audited/sglang-source-files
```

Root must confirm prior training/inference resources are fully released before
approving this phase, because image pull/unpack can contend with host I/O even
without GPUs. Review frozen hashes and use the copied controller's explicit
`--execute` command. After cleanup, independently verify exact image content and
completed mounted rootfs cache on the same host. Keeping an idle image cache does
not retain running compute. A later GPU test still requires a separate budget and
approval; cache eviction never authorizes extending that GPU lease.

## One-A100 native smoke

`gpu_smoke.py` prepares a new plan locally using the exact successful CPU HOST
fixture, CPU launcher/plan/proof, independent cleanup proof and mounted image-cache
proof. It verifies unchanged runtime files/native source hashes. Review the frozen
plan/controller/payload/guards before explicit `--execute`; preparation claims
nothing. The fixed contract is one NVIDIA A10080GB, 8 CPU/32GiB HOST/GVISOR,
900 seconds including fixture upload and startup, explicit identity disabled,
180 seconds without native/test progress, and 120 seconds reserved for cleanup.
Cold startup is bounded by300seconds, restarts180seconds, and updates30seconds.

```bash
python scripts/sglang/gpu_smoke.py --out /tmp/sglang-gpu-plan \
  --source-root /path/to/this/sdk \
  --guard-root /path/to/reviewed/scripts/weight_sync \
  --audited-source-root /path/to/audited/sglang-source-files \
  --cpu-proof-root /path/to/successful/released/cpu-host-plan
python /tmp/sglang-gpu-plan/gpu_smoke.py --execute \
  --out /tmp/sglang-gpu-plan --env-file /path/to/ftqa.env
```

The detached guard is armed before any unique fixture-volume metadata/upload.
An upload subprocess is born inert, then bound by phase/PID/process group/start
identity before authorization. Metadata creation uses one explicit30second RPC with no retry interceptor. A
durable create-inflight marker precedes the RPC; an ambiguous response keeps the
guard armed through its deadline plus60seconds of repeated unique-name deletion
and absence checks after the upload writer has stopped. A successful create ACK
uses the fast cleanup path. No create marker means no deletion of the planned
name, so a pre-existing random-name collision is protected. The <=8MiB fixture is mounted at
an immutable volume version and checked against the exact CPU guest bundle hash.
Every pointer/corruption change happens in a guest-local copy. This smoke validates
native SGLang and supervisor behavior; live ChalkFS ref-follow evidence belongs
to the separate end-to-end workload tests.

The payload attempts managed cold v0, real stream drain into v1, actual client
disconnect/drain into v2, no-op, PIN rollback to0, corrupt payload rejection before
mutation, an injected lost acknowledgement after actual successful native disk
mutation, fail-closed restart, and separate fresh native v0/v1/v2 oracle instances
on the same GPU. Eight declared token-ID prompts use deterministic generation and
top-five logprobs; oracle tolerance stays1e-3 and both consecutive version changes
must exceed0.01. Test-only gate observations require genuine active requests before
drain and zero after; no artificial native delay creates the overlap. Product code
contains no fault hooks. An unavailable native image/device/kernel contract fails
this attempt; there is no in-lease package/model/image troubleshooting.

`gpu-proof.json` and bounded guest/log progress retain completed scenarios if the
budget is insufficient or any scenario fails. Partial scenarios never imply a full
pass. Managed/native process groups are identity-bound, stopped and checked,
including orphaned members; preparation processes are joined before checkpoint
cleanup. Compute teardown verifies API, retained CR UID pods, exact HOST runsc and
hypervisor-containerd absence. The upload writer is stopped before its unique
volume is deleted. Cleanup uncertainty leaves the detached guard armed.

After release, `verify_host_release.py --out /path/to/frozen/plan` performs fresh
read-only exact CR/owner-UID pod/native/process checks and, for GPU plans, confirms
the unique fixture volume and upload writer are absent. It writes a new independent
proof without replacing a prior proof.

### Fixed cache diagnostic after a numerical failure

Prepare a separate `--cache-diagnostic` plan to isolate the same-model native cache
baseline. Its fixed lease is600seconds including upload/startup/export, with the
same one-A100/8CPU32Gi contract and120seconds reserved for cleanup. It loads only
oracle-v0 and compares its initial prompt sequence against two warm-prefix repeats,
an explicitly acknowledged native cache flush followed by the cold sequence, and
a separately started fresh native process. No weight reload occurs in this mode.
The1e-3 threshold stays fixed. All four comparison results are retained even when
a warm repeat fails; this diagnostic cannot imply that native weight sync passed.

Every comparison saves raw observed/reference values per prompt before raising,
plus the exact maximum logprob error and prompt/generated-token/token location.
Native response usage/cache fields, bounded cache configuration/state and the
flush acknowledgment are retained separately. The <=2MiB compressed diagnostic
bundle is hash-bound in the proof and exported with the same bounded reads before
compute teardown, even after a numerical failure. Health-poll access-log growth
does not count as meaningful progress. No new lease is claimed until its separate
frozen diagnostic plan has been reviewed after previous cleanup.

## Explicit deterministic/triton diagnostic candidate

The previous default BF16/flashinfer/radix same-v0 diagnostic **failed**: both warm
repeats differed by0.004029750823974609 at the fixed1e-3 threshold; explicit flush
and fresh process comparisons were0.0. Keep those attempts unchanged.

The separately reviewed read-only pinned-image attestation matched all13 audited
configuration/attention/invariance source modules at build commit
`297e54fd105e974cdf401e5b4d20253a22c16f90`. Its exact proof SHA256 is
`c5e1fa95706622eb77c30f5ff886c79a5ff4f2fec51edb854850504eed9fdd65`.
This supports testing the explicit experimental adapter profile
`diagnostic_profile="deterministic-triton"`, which adds only
`--enable-deterministic-inference --attention-backend triton`. Ordinary engine
arguments still reject those flags and arbitrary backend overrides. Defaults,
backend runtime catalog and FULL_WEIGHTS remain disabled/unchanged.

Prepare a distinct candidate using `gpu_smoke.py --deterministic-triton
--source-attestation /path/to/exact/cached-attestation/stdout.json
--capacity-source /path/to/shared/reviewed/profile_capacity.py` plus the same
four source/proof roots. It freezes the exact attestation, rechecks all13 native
module hashes in the guest and permits only the experimental adapter overlay
change from the successful CPU fixture proof. No new model or image is needed.
Use `preview_gpu_plan.py` to validate/compile the exact frozen command before
parent review. Preparation and tests claim nothing.

This combined native diagnostic retains the900second one-A100/8CPU/32GiB ceiling,
120second cleanup reserve, fixed1e-3 comparisons and unchanged lifecycle guards.
Every managed/fresh native startup must report deterministic=True,
attention=triton, sampling=pytorch, radix enabled, graphs disabled and weight
cache off before samples are accepted; absent/conflicting settings fail closed.
The same-v0 cold/two warm/flush/fresh comparisons precede full updates/drain,
disconnect, no-op, PIN rollback, corruption refusal, lost ACK after actual native
mutation, restart and fresh v0/v1/v2 oracles. Numerical failures retain raw errors
and allow remaining diagnostic cases to run; structural/native/lease failures
stop safely. Unreached scenarios remain explicit and never count as passes.
This profile has not yet proved native kernel compatibility or determinism.
The combined phase may exhaust its fixed budget; that is a failed/incomplete run,
not permission to extend the lease or loosen checks. Review the exact new freeze
and fresh host capacity before any claim; independently prove all release paths
before another phase. The shared lifecycle sources are reused without a new fork.

The absolute execution clock starts at the first line of execute(), before
validation/import/auth/discovery, and includes fixture upload, native startup,
scenario work and export. Immediately before sandbox create, reuse the exact
shared capacity helper's read-only collect_raw() inventory and save it; require
8CPU/32GiB host/ancestor-cgroup headroom and8GiB free at relevant host/hypervisor
writable paths. The original full-model helper's120GiB gate remains unchanged.
The tiny frozen fixture contract (<=20MiB unpacked, <=8MiB compressed), stream/
checkpoint copies and bounded raw artifacts fit a128MiB fixed-data reserve;
6GiB is reserved for native/JIT paths with about1.875GiB slack. Diagnostic Triton,
Inductor, CUDA and XDG cache directories are bound to owned guest-local paths.
Startup/prompt-boundary ordinary-path samples fail above6GiB; this is **not a
hard quota** and cannot bound open-unlinked files or every native write outside
those paths. Fresh API placement/one-free-A100 evidence and independent cleanup
remain required parent execution gates. Reached structural failures are marked
failed; later scenarios remain not_reached.
