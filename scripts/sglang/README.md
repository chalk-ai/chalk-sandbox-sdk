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
