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
credentials are passed into the guest. HF offline flags prevent implicit model
fetches. A second execution of the same plan fails its exclusive execution lock.

The payload records actual dependency versions, requires SGLang0.5.23,
Torch2.14.1/CUDA13 and Transformers5.19.0 matching the audited source pins, checks
five native control/loader source hashes, and never initializes CUDA. A different
installed stack is a compatibility blocker for review; the script does not loosen
requirements. It generates a1,246,464parameter BF16 Llama/WordLevel-tokenizer
fixture with no HF download, keyframe plus two absolute patches, independent full
oracles and bitwise materialization checks. The <=20MiB bundle is exported with
short read-only64KiB reads and verified against the durable guest proof.

Finally, the launcher stops/verifies the exact CPU sandbox/container CR/pods,
deletes no external volumes, disarms the guard only after verified cleanup, and
retains small local proof/fixture/log files. On ambiguous create or failed cleanup,
the detached guard stays armed and reconciles the unique owned name. Preparation
and unit tests claim nothing. A successful CPU proof does not authorize a GPU
claim: submit a separate fixed-budget one-A100 plan after all prior resources are
released, with a fresh-reference output oracle and verified teardown.
