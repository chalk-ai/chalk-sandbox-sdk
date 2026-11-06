# Inactive Kimi TP8 sparse experiment

These scripts do not create resources, download models, install dependencies or
start a server. An external reviewed controller must own the spot allocation,
immutable deadline, server process group, runtime validation and cleanup.

The experimental runtime dependency is the separate direct resident sparse worker
change. Legacy `ws_apply_dir` and upstream checkpoint-loader fallback are excluded.
The minimal overlay uses real SDK package initializers and only the pure Python
worker/codec modules needed here; this is not full installed SDK integration.

Run CPU checks with an existing Torch/NumPy/pytest/xxhash/zstandard environment.
The strict serialization regressions additionally use msgspec:

```sh
python -m pytest -q tests/test_vllm_sparse_experiment.py
python -m pytest -q tests/test_vllm_sparse_result_summary.py
python -m pytest -q tests/test_vllm_sparse_rpc_metadata.py
python -m ruff check scripts/vllm_sparse tests/test_vllm_sparse_experiment.py
```

`python -m scripts.vllm_sparse.prepare --help` documents local deterministic overlay
preparation. Inputs include the reviewed runtime source checkout, upstream source
hash manifests and exact pinned model config. It refuses to overwrite an output.

Inside the pinned image, run `runtime_gate` before any model download. It verifies
the overlay/import roots, native vLLM source bytes, installed dependency presence,
Python3.12 and exactly `vllm==0.33.0` or `vllm==0.33.0+rocm100`, actual CPU codec
API and HIP build. The latter is the release's ROCm 10.0.0 packaging variant;
development builds, other local versions and CUDA variants remain rejected.
Both accepted strings still require the frozen image and exact native source
hashes. The actual installed version remains a recorded runtime fact; support for
the packaging variant does not establish that the image contains it. The explicit
`--native-device-check` additionally requires eight gfx950 devices,230GiB free each
and tiny indexed BF16 kernel readback. Missing dependencies fail; no install fallback.
Record the actual package versions rather than guessing them from image labels.

Start the frozen loopback server externally using
`scripts.vllm_sparse.audit_extension.SparseAuditExtension`, then run `pod_script`
with `--execute-reviewed`, exact model config path and expected SHA256. It consumes
the existing adapter's `AMD_EXPERIMENT_*` model, revision, run-dir and URL variables.
All workers must pass effective eager/cache/parallelism/offload configuration checks.

Before mutation, unchanged tensor snapshots must repeat exactly and eight synthetic
generation prompts must repeat with exact token identities and fixed1e-3 logprob
tolerance. The criterion is persisted before updates and never loosened afterward.
Then actual codec sparse streams drive A→B→C, inverse-A, no-write commit replay and
explicit corrupt-checksum refusal. Each prepare rank is bound to the original
worker/process/model/storage identity before any commit.

The norm is replicated and the unquantized head has explicit contiguous vocabulary
row shards. Small norm bytes and chunked head masked hashes prove changed and
unchanged bytes of these inspected tensors only. The full model is never cloned to
CPU. Native commit instrumentation rejects full loaders/where/factories and records
indexed writes to resident storage; it is not a general memory quota.

Any uncertain commit is a failed replica requiring immediate controller termination;
never retry, reload or serve it as a passing sparse experiment. The final scope record
keeps stream drain and fresh native B oracle explicitly unproven. Generation after
an inverse update does not replace a separate fresh-B reference.

No native/AMD compatibility result is claimed by these source/CPU tests.

Audit RPC metadata contains exact built-in scalars, lists and string-keyed
dictionaries. Torch's `TorchVersion` string subclass is explicitly converted to
`str`; unknown objects fail before the strict msgpack boundary, without a pickle
fallback. A later pinned native attempt reached a loaded, healthy Kimi server and
passed the effective worker profile, then failed the first snapshot's original
TorchVersion serialization before baseline generation or sparse writes. The CPU
regression reproduces that rejection with actual TorchVersion/msgspec and checks
the corrected full snapshot response. Native sparse proof still requires a new run.

Future harness exceptions preserve an explicit `failure.json` and update the new
attempt's scope to `failed`, retaining partial evidence and unproven fields. Earlier
attempt receipts remain as recorded, including their partial `running` scopes.

After exporting a completed `sparse-proof` directory, the stdlib-only offline
summarizer checks exact norm bytes, selected head bytes and unchanged-head hash
consistency across all eight ranks, plus resident identity, three all-rank prepare
barriers bound to exported manifests and matching commits, commit/replay order,
exact indexed-write instrumentation and explicit checksum refusal. The inspected
norm and head must have distinct positive resident storage pointers:

```sh
python -m scripts.vllm_sparse.summarize_result --root /path/to/exported/sparse-proof --out /path/to/new-summary.json
```

It never contacts a server, mutates input receipts, or overwrites its output. A
non-passing harness scope returns a failed summary and nonzero exit; absent or
inconsistent evidence raises an error. Its `passed` field concerns these exported
byte/control receipts only. It trusts worker instrumentation and does not
authenticate image/source, verify generation samples, establish cleanup or prove
stream drain, fresh native B, or full SDK integration. The tests use synthetic
receipt files and tiny byte arrays; they do not constitute native evidence.

The first pinned AMD spot attempt (`kimi-vllm-sparse-20261106-0100-2c327e`) failed
before the native harness ran. Outer image attestation raised
`PackageNotFoundError: No package metadata was found for aiter`. This establishes
missing distribution metadata, rather than a missing import module. The remote
attestation interpreter acknowledged exit 1; the frozen synchronous controller
then entered cleanup before dispatching lifecycle selftest, native inspection,
model preparation or experiment. No model/sparse result is claimed. The offline
summarizer is unsuitable for classifying that prelaunch failure: it requires
completed sparse receipts and must not infer a native launch or absence from
missing files. Resource release and provider reclamation require independent
controller evidence.
