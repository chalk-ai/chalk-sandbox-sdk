# DeepSeek SGLang resident sparse proof

This opt-in harness uses the pinned AMD SGLang offline `Engine` to generate real
tokens and update two resident BF16 parameters. It preserves the existing dense
SGLang adapter. It does not enable a serving catalog entry or implement packed
expert updates, a production HTTP supervisor, live trainer publication or XOR
decoding against GPU parent bytes.

Prepare all source on the workstation before a root-owned allocation:

```sh
python -m scripts.sglang_sparse.prepare \
  --observed-source /path/to/archived/actual-source/python/sglang \
  --supplemental-source /path/to/pinned/flattened/source \
  --model-config /path/to/pinned/config.json --out /new/candidate
```

The output archive has minimal package initializers, the existing codec/format,
the resident module and two proof helpers. The full SDK is not installed into
the image. The plan records image, source provenance, exact hashes and startup
settings. Review and freeze it with the separate watchdog/controller before
claiming demand. Native image/package/hash/device/indexed-kernel checks precede
the model download; missing dependencies fail rather than installing packages.

After staging under `/workspace/proof/overlay`, use the frozen profile's
environment and set `PYTHONPATH=/workspace/proof/overlay`. The commands are:

```sh
python -m scripts.sglang_sparse.driver --overlay /workspace/proof/overlay \
  --out /workspace/runs/deepseek-sglang-sparse-proof --preflight-only
python -m scripts.sglang_sparse.driver --overlay /workspace/proof/overlay \
  --out /workspace/runs/deepseek-sglang-sparse-proof \
  --model-path /workspace/models/deepseek-ai--DeepSeek-V4-Flash-0731/7872f01b1d1fe23eabc4c98b48bffcef5a386062 \
  --deadline-env AMD_NATIVE_PHASE_DEADLINE
```

Only the outer controller sets the absolute phase deadline. Startup is bounded
to600seconds; no next phase begins without its required remaining window. The
driver calls `Engine.shutdown` in `finally`, with bounded cleanup; the outer
Linux process-group supervisor independently reaps descendants and releases
the allocation. No lifecycle or GPU allocation commands live in this harness.

The proof admits exactly TP8/PP1/DP1/CP1, gfx950, the pinned DeepSeek model, final
norm[4096] and unquantized vocabulary head global[129280,4096]. All native
layouts and storage are checked before writes. Coordinates hit the first/last
element of each local parameter; head ownership is checked at every TP shard.
Preparation decodes bounded sparse absolute payloads and stages all values.
Commit requires all eight distinct stable identities and the manifest digest,
then performs only indexed resident writes. Replay performs no writes.

The offline workload is single-controller, synchronous generation: no other
clients issue requests. Every mutation requires native scheduler idle+paused,
and a device fence. Cache flush succeeds before native resume. This tests native
pause/cache behavior with zero outstanding requests; it does not prove HTTP
admission, disconnected-stream drain or preparation concurrent with inference.

The spawn override uses SGLang's explicit `run_scheduler_process_func` extension
point. One collective RPC method writes per-call/per-rank receipts atomically.
Ordinary operation failures become receipt data so every original rank reaches
the pinned scheduler RPC barrier. Host success requires all eight bound receipts;
the aggregate RPC acknowledgement alone cannot advance the proof.

The sequence is A/repeatA, sparseB, replayB, inverseA, replayInverse and corrupt
checksum refusal. Each leg saves generation, chosen tokens, top-five logprobs,
transaction receipts and snapshots. Full selected-target reads provide an
explicit test oracle: complete and masked-untouched SHA256 plus selected BF16
bytes. Their CPU copies and timings are validation overhead, not production
delta reconstruction or a speed claim. They do not audit other model weights.

Generation comparisons require exact text, chosen tokens and top-token keys,
finite values and maximum1e-3 logprob error. Differences remain recorded failures
while this explicitly diagnostic harness finishes byte/control evidence; exit2
reports such a failure. Schema/nonfinite/control/byte errors fail immediately.
`full_smoke_accepted` is alwaysfalse. Any uncertain write closes the experiment;
restart is required. Corrupt payloads must be explicitly refused by all ranks
before mutation, followed by exact original bytes and generation checks.

Run CPU checks with an existing environment:

```sh
python -m pytest -q tests/test_sglang_resident_sparse.py tests/test_sglang_sparse_harness.py
```

The native module's tests cover indexed writes, no-write replay, stale/invalid
manifests, sticky uncertainty, topology and layout rejection. Harness tests run
the complete transaction sequence with the real codec and small CPU tensors;
they do not claim native AMD execution.
