# Inactive Kimi TP8 sparse experiment

These scripts do not create resources, download models, install dependencies or
start a server. An external reviewed controller must own the spot allocation,
immutable deadline, server process group, runtime validation and cleanup.

The experimental runtime dependency is the separate direct resident sparse worker
change. Legacy `ws_apply_dir` and upstream checkpoint-loader fallback are excluded.
The minimal overlay uses real SDK package initializers and only the pure Python
worker/codec modules needed here; this is not full installed SDK integration.

Run CPU checks with an existing Torch/NumPy/pytest/xxhash/zstandard environment:

```sh
python -m pytest -q tests/test_vllm_sparse_experiment.py
python -m ruff check scripts/vllm_sparse tests/test_vllm_sparse_experiment.py
```

`python -m scripts.vllm_sparse.prepare --help` documents local deterministic overlay
preparation. Inputs include the reviewed runtime source checkout, upstream source
hash manifests and exact pinned model config. It refuses to overwrite an output.

Inside the pinned image, run `runtime_gate` before any model download. It verifies
the overlay/import roots, native vLLM source bytes, installed dependency presence,
Python3.12/vLLM0.33, actual CPU codec API and HIP build. The explicit
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
