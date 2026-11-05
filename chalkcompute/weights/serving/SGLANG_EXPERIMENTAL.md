# Experimental SGLang serving

Install the SDK's `serving` extra into the pinned SGLang runtime, mount a Chalk weight
stream at `/weights`, and launch:

```bash
chalk-live-serve --engine sglang-experimental \
  --sglang-profile deterministic-triton --weights /weights -- \
  --context-length 8192 --mem-fraction-static 0.5
```

The CLI uses `deterministic-triton` by default for this engine. Native SGLang listens
on loopback; the supervisor exposes the OpenAI routes on `CHALK_LISTEN_ADDR` (default
`:8000`). Responses report the applied stream version in `X-Chalk-Policy-Version`.
The SDK keeps SGLang optional: installing the base SDK does not install or replace
Torch, SGLang or an accelerator runtime.

The supported experimental contract is **SGLang 0.5.23**, upstream commit
`297e54fd105e974cdf401e5b4d20253a22c16f90`, with dense unquantized BF16
`LlamaForCausalLM`, TP/PP/DP/EP1, deterministic inference, Triton attention, PyTorch
sampling, radix cache enabled, weight cache off and CUDA graphs disabled. The
specific CUDA image used in the existing tiny A100 diagnostic is:

```
lmsysorg/sglang@sha256:354700f4404ab0b8dcb493bc61d5d11e7c2a91f3a7babb4efe741cb75d4b51c0
```

That image contains Python3.12.16/Torch2.14.1+cu130. Other versions, model families,
quantization, parallelism, speculative decoding, shared caches and ROCm/MI355X need
separate validation. Only `--context-length` and `--mem-fraction-static` pass through
to SGLang. Runtime `SGLANG_` overrides are rejected; only the exact pinned image's
build provenance and `SGLANG_RUST_BUILD_MODE=never` are allowed. The backend
SGLang/ROCm runtime catalog remains disabled.

Each update reconstructs one complete final checkpoint from the verified mounted
keyframe/patch chain **before closing admission**. The supervisor then drains native
work, pauses generation, reloads synchronously, flushes cache and checks exact native
model path, format, target version and one-scheduler runtime settings. It resumes
native generation before acknowledging the applied stream version. A downstream
disconnect remains admitted until native EOF. Uncertain mutation, missing/version-
mismatched acknowledgement or resume failure closes the replica; restart is required.
Corrupt payloads fail during preparation before native mutation.

This is a dense checkpoint reload baseline; it does not apply sparse GPU deltas.
Reserve host memory for full replay/decoding and local disk for bootstrap, current
and candidate checkpoints (up to three full copies). No CPU model remains resident
between updates. Bootstrap/tokenizer assets and uncertain candidates remain available
until the native process group stops. Cancelled preparation writers are joined before
removing their output, after stopping GPU workers.

## Validation scope

The earlier explicit deterministic-triton diagnostic (PR611/head8364ef18) passed all
ten tiny one-A100 comparisons with maximum logprob error0.0 at the unchanged1e-3 gate:
streaming/disconnect drain, updates, no-op, backward PIN, corrupt-payload refusal,
lost native ACK/fail-closed restart and fresh-native versions0/1/2. All resources were
independently released. It used guest-local copies of an immutable fixture, so it does
not prove live ChalkFS publication, trainer durability, AMD behavior, full-model latency
or general numerical determinism.

The older default BF16/FlashInfer/radix smoke and warm-cache diagnostic **failed**;
those results are unchanged. CLI exposure and new native identity checks in this PR
are CPU tested; native validation of this exact source is pending a separately reviewed,
frozen run. The previous diagnostic is evidence for its exact source/profile, not a
pass for later code. Do not enable a backend catalog entry or FULL_WEIGHTS based on it.

Use `PYTHON=/path/to/existing/python scripts/check_weight_serving.sh` for reusable CPU
checks. The separate guarded native harness preserves raw samples, fixed1e-3 criteria,
immutable deadline and independent teardown; its sources and exact executed overlay
must be frozen/reviewed before every resource claim. AMD tests additionally need a
validated gfx950/ROCm image and an idle scale-down watchdog; none is claimed here.
