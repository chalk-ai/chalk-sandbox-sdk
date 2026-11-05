# Experimental SGLang full-checkpoint reload

This is an opt-in correctness baseline for SGLang **0.5.23**, audited at upstream
commit `297e54fd105e974cdf401e5b4d20253a22c16f90`. It has CPU tests with a fake native
HTTP server. It has **not** been validated on a GPU, ROCm image, or Chalk
hypervisor. The backend runtime catalog remains unavailable for SGLang/ROCm.

The first supported input contract is a dense, unquantized `LlamaForCausalLM`
checkpoint whose config and actual tensor catalog are BF16. TP, PP, DP and EP
are fixed to one; CUDA/HIP graphs are disabled. Quantized models, MoE, shared or
derived weight caches, other model families, speculative decoding, and other
parallel modes require separate adapters and validation. The only passthrough
arguments are `--context-length` and `--mem-fraction-static`. `SGLANG_` environment
overrides are rejected to keep these restrictions effective.

```bash
chalk-live-serve --engine sglang-experimental --weights /weights -- \
  --context-length 4096 --mem-fraction-static 0.8
```

Install the image's compatible pinned Torch/SGLang stack separately. Installing
the SDK does not choose or replace an accelerator runtime. The native server
listens on loopback; the existing supervisor exposes the OpenAI routes.

For each desired version, the separate `FullCheckpointSyncer` walks the complete
keyframe-to-target chain and reconstructs **one** final checkpoint on local disk
before closing admission. It verifies all payloads, actual keyframe tensor
names/shapes/dtypes, config architecture, and patch compatibility. CPU tensors
are released after writing that checkpoint; no continuously resident CPU copy
of the preceding model is retained. Mounted immutable version files are the
source; this path does not call Volume APIs or reread mutable `CURRENT` during
replay. Bootstrap starts the final checkpoint directly.

After draining external work, the update performs native `in_place` pause, one
synchronous `update_weights_from_disk` call with `keep_pause` and `flush_cache`,
an explicit acknowledged cache flush, and native resume. The applied version
advances only after successful resume. The TP1 disk result must report JSON
`success: true` and zero paused requests. Pause/resume must return `status: ok`;
every control response must be HTTP 200. A timeout, cancellation, invalid
response, reload failure, cache-flush failure or resume uncertainty after
mutation fails the replica closed and requires process-group restart. Native
messages claiming rollback do not establish recovery.

Bootstrap assets stay available until engine shutdown because the tokenizer may
refer to them. The current checkpoint stays available until replacement; an
uncertain candidate stays available until engine shutdown. Superseded successful
checkpoints are removed. Reserve local disk for bootstrap, current and candidate
checkpoints (up to three full copies), and host memory for full replay plus
checkpoint decoding. Disk or preparation errors leave the live engine untouched.
This baseline still copies a full checkpoint into the engine; it does not claim
sparse GPU writes, improved pause latency, or measured performance.

## Proposed device/image validation (requires a separately reviewed run)

1. Resolve an immutable image digest and record its SGLang commit/version, Torch
   build, `torch.version.hip`, gfx target, and userspace ROCm paths. The audited
   upstream ROCm Dockerfile has gfx950 flavors, but their existence does not prove
   compatibility with the Chalk hypervisor's injected ROCm 7.1.1. Inspect image
   metadata and dependencies before claiming GPU compute. Keep CUDA-specific
   library-path injection out of a ROCm image; use `torch.cuda` APIs for ROCm
   where required by PyTorch.
2. First use a tiny local BF16 Llama checkpoint, TP1, on the proposed runtime.
   Check cold startup, deterministic before/after generation or logits against a
   fresh native load of the exact target, sparse then dense publications,
   unchanged target, backward PIN and version zero. Exercise active streaming
   drain, downstream disconnect, invalid payload before mutation, injected native
   reload/resume failure, fail-closed admission and clean restart recovery.
3. Save immutable source/target IDs, image digest, native settings, outputs,
   correctness comparisons, replay/apply/pause timings, full disk transfer bytes,
   peak host/GPU memory and cleanup evidence. CPU fake-server tests alone cannot
   establish loader layout, native cache correctness or device compatibility.
4. Review those artifacts before an 8B run or enabling a catalog entry. An A100
   result establishes only that CUDA image/device pair. MI355X needs its own
   gfx950/ROCm image evidence. TP/EP and quantized models remain unavailable.

For a proposed AMD run, report its node count, ~$30/node-hour cost, hard deadline,
idle watchdog and exact teardown before allocation. The watchdog must be ready
before claim, scale nodes down after >10 minutes without meaningful work, and
release every owned sandbox, volume and node on completion/failure/cancellation.
No AMD or other GPU resources were claimed for this implementation.
