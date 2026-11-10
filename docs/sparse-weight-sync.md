# Sparse weight updates with user-defined layouts

The publisher describes weights in checkpoint coordinates. A user-supplied
layout describes how those tensors occupy the inference engine's resident
parameters. The same checkpoint version can have different layouts for vLLM,
SGLang or a different engine implementation. The SDK contains no Qwen dimensions,
layer counts, parameter counts or model-name checks in this path.

The interfaces borrow the declaration style of Hugging Face's
[`WeightConverter`](https://github.com/huggingface/transformers/blob/4816e89f1abf9c8bd7778822eea872b83da256ec/src/transformers/core_model_loading.py#L1200)
and NVIDIA's [Megatron Bridge mappings](https://docs.nvidia.com/nemo/megatron-bridge/latest/apidocs/bridge/bridge.models.conversion.param_mapping.html).
Those libraries perform conversions on complete tensors. Here the recipe
compiles into sparse coordinate transforms: concatenate describes addressing
and does not concatenate full tensors on every update.

## Describe and bind a layout

Derive the logical schema from the complete initial keyframe manifest, without
loading values. For an engine packing Q/K/V and gate/up along dimension zero:

```python
from pathlib import Path
from chalkcompute.weights import Concatenate, TensorMapping, WeightLayout, WeightSchema

schema = WeightSchema.from_manifest(initial_manifest)
layout = WeightLayout(
    rules=[
        TensorMapping(
            sources=[
                "model.layers.*.self_attn.q_proj.weight",
                "model.layers.*.self_attn.k_proj.weight",
                "model.layers.*.self_attn.v_proj.weight",
            ],
            target="model.layers.*.self_attn.qkv_proj.weight",
            operations=[Concatenate(dim=0)],
        ),
        TensorMapping(
            sources=[
                "model.layers.*.mlp.gate_proj.weight",
                "model.layers.*.mlp.up_proj.weight",
            ],
            target="model.layers.*.mlp.gate_up_proj.weight",
            operations=[Concatenate(dim=0)],
        ),
    ],
    match_by_name=True,
).bind(schema)

# Only local files are written; no compute or volume is allocated.
Path("weight-schema.json").write_bytes(schema.to_json())
Path("weight-layout.json").write_bytes(layout.to_json())
print(layout.sha256)
```

This is a user-owned recipe, including the arrangement used by the measured
Qwen fixture. It is not an architecture detector. For unpacked parameters with
matching names, `WeightLayout(rules=[], match_by_name=True).bind(schema)` works.
The compiler expands wildcards, rejects ambiguous or incomplete mappings and
checks the actual destination shapes/dtypes. Rebind the recipe when the logical
schema changes; the updater does not need a new implementation for each size.

Helpers cover direct/renamed mappings, concatenation, splitting, transpose,
reshape and grouped interleave. Some QKV layouts interleave each Q-head group
with its K/V heads; the user supplies that grouping to `Interleave`. There is
no fallback that guesses packing or invokes a full weight loader. Only supported
element-preserving operations compile into sparse writes. Quantization,
arithmetic transforms, custom callbacks, tied/aliased allocations and distributed
sharding require additional contracts and are not enabled by a model name.

## Registry-compatible contracts

Logical schema and runtime layout have separate versioned JSON encodings and
SHA256 identities. A bound layout names its exact source-schema hash. Builders
serialize plain data; loading it neither imports Torch nor executes callbacks.
The existing stream `architecture_hash` retains its meaning: canonical model
config plus logical names/shapes/dtypes. Schema/layout hashes are additional
contracts, not new meanings for that field.

The logical schema belongs with an immutable checkpoint artifact. A deployment
selects a separately identified layout and compatible runtime. One registered
model version can have multiple runtime bindings. Runtime pointers and process
IDs belong only in local binding receipts. Registry model versions, weight-stream
versions and ChalkFS snapshots remain separate identities.

Merged [#51133](https://github.com/chalk-ai/chalk-private/pull/51133) and
[#51274](https://github.com/chalk-ai/chalk-private/pull/51274) provide immutable
artifact snapshot references. Typed schema/layout references are future additive
registry/deployment work. Keep sidecars in an immutable snapshot meanwhile;
mutable artifact metadata alone is not the authority. The existing inference
input/output signature is not a weight schema. This stack does not change
protocols, generated bindings, promotion, FULL_WEIGHTS experiment admission or
trainer cancellation fencing.

## Serving

The opt-in profiles currently require unquantized BF16, TP1 and whole unaliased
contiguous parameters on one CUDA device. Accepting a layout does not qualify an
untested model, GPU, cache arrangement or engine build. Send inference through
the supervisor so request drain surrounds indexed updates and cache reset.

In the corresponding pinned engine image with the reviewed SDK wheel installed:

```sh
chalk-live-serve --engine vllm-sparse --weights /weights \
  --weight-layout /deployment/weight-layout.json \
  --weight-layout-sha256 "$WEIGHT_LAYOUT_FILE_SHA256"

# Alternative, in the SGLang image:
chalk-live-serve --engine sglang-sparse --weights /weights \
  --weight-layout /deployment/weight-layout.json \
  --weight-layout-sha256 "$WEIGHT_LAYOUT_FILE_SHA256"
```

The CLI hash pins exact file bytes. `layout.sha256` identifies canonical JSON;
these are identical for files written with `to_json()`. For reformatted JSON,
hash the actual file bytes for the CLI pin. The adapter captures and verifies
the file before worker launch. Later path changes cannot redefine that running
binding. Native ACKs must match the canonical layout, schema, version, manifest
and resident identity. A different layout requires a fresh replica.

Mount the stream's mutable ref at `/weights` so publication/PIN changes become
visible; a pinned immutable volume mount cannot follow versions. Pin the layout
sidecar independently. The complete initial keyframe is loaded once and every
logical tensor is byte-audited before readiness. Subsequent sparse updates
preserve resident allocations. Existing `WeightPublisher` commits payloads,
manifest and CURRENT atomically in one commit.

Selecting C while A is live replays the necessary parent chain; the caller need
not pin B first. The sparse element/byte bounds and periodic keyframes still
constrain eligible publications. Dense patches, keyframes, excessive lag and
backward PIN require restart from a suitable complete keyframe. Restart alone
does not convert a dense patch. Higher-version sparse patches may carry revert
values. Ordinary `vllm` and `sglang-experimental` modes retain their behavior.
Torch and engines remain optional; default SDK dependencies are unchanged.

## Review and runtime contracts

| PR | Scope |
| --- | --- |
| [625](https://github.com/chalk-ai/chalk-sandbox-sdk/pull/625) | Schema/layout, coordinate compiler, generic resident updater and admission. |
| [626](https://github.com/chalk-ai/chalk-sandbox-sdk/pull/626) | vLLM bridge, layout pinning and sparse-only supervisor policy. |
| [627](https://github.com/chalk-ai/chalk-sandbox-sdk/pull/627) | SGLang scheduler bridge with the same layout/ACK contract. |
| [628](https://github.com/chalk-ai/chalk-sandbox-sdk/pull/628) | This guide, CPU verification and public output verifier. |

Runtime package/native-source contracts remain pinned; a general mapping does
not make arbitrary engine upgrades compatible. Tested starting images:

| Engine | Image | Packages |
| --- | --- | --- |
| vLLM 0.33.0 | `vllm/vllm-openai@sha256:91ee972ab751fb7180bc71c175865e36b091686600dca783d52b2511399810b3` | torch 2.14.0+cu130; transformers 5.20.2 |
| SGLang 0.5.23 | `lmsysorg/sglang@sha256:f7876bef696ccf9cf75a2bf7ccb82970f5fac577f082fb8ca95c9a59d2160eb5` | torch 2.14.1+cu130; transformers 5.19.0 |

Both use safetensors 0.8.0, xxhash 3.6.0 and zstandard 0.25.0. Install the
reviewed wheel without upgrading pinned native dependencies.

## Evidence and release qualification

Earlier native Qwen3-8B harnesses passed 0.1%/1% synthetic sparse changes on
vLLM and SGLang with complete weight checks, stable identities and fresh output
references. The later paired 1% comparison fully passed for vLLM: median native
RPC 4.161s full versus 2.561s delta. SGLang completed six update/weight audits
but failed its fixed time-budget guard before the fresh output reference; that
comparison remains incomplete. The synthetic bundle was 128.9MB versus16.38GB
full weights; CPU diff/encoding took about20s. Warm-memory native measurements
do not prove end-to-end rollout speedup or training quality.

Those harnesses did not execute this refactored compiler or public HTTP bridges.
Exact-artifact GPU qualification remains outstanding. Keep serving PRs draft
until it passes; CPU tests on independent non-Qwen shapes do not qualify models
or hardware. Run the reusable CPU checks with `scripts/check_weight_serving.sh`.

For each engine, freeze wheel/source hashes, schema/layout bytes and hashes,
runtime image, model/fixture revision, commands and ownership before allocating.
Use an absolute deadline including preparation and cleanup, a watchdog and
independent release verification. Preserve failures; do not extend a lease or
loosen correctness thresholds. The guide and checker allocate no resources.

Qualification must include:

1. Exact reviewed SDK, a real mutable ChalkFS mount, public supervisor HTTP,
   complete startup audit and a nonzero initial keyframe version.
2. User recipes for the measured fixture and a distinct non-Qwen layout; A
   pinned while B/C publish, real streaming/disconnect drain and A-to-C replay.
3. Independent full CPU reconstruction/resident byte checks, stable process/
   model/parameter/storage identities and no full loader after startup.
4. Fresh separately loaded target output, raw responses, exact top-five deltas
   and fixed `1e-3` logprob tolerance.
5. Corrupt payload/layout refusal, schema/hash mismatch, no-op, exact replay,
   higher-version revert/reapply, lost ACK/worker death, cache refusal and
   dense/keyframe/backward-PIN restart from an appropriate complete keyframe.
6. Private controls blocked on the public port and independent release of owned
   API/sandbox/native/volume resources and local writers/watchdogs.

The model-independent checker consumes an existing public endpoint and a fresh
reference response; it neither drives publications nor proves native identities:

```sh
python scripts/check_sparse_weight_http.py \
  --url http://127.0.0.1:8000 --expected-version 2 \
  --request-json request.json --reference-json fresh-c-response.json \
  --output attempt-1-c-public-oracle.json
```

Use a completions request with `temperature:0`, `max_tokens:16`, `logprobs:5`,
`stream:false`, `n:1`. Set `CHALK_WEIGHT_TEST_BEARER` locally if needed. The
output path must be new. Raw actual responses survive failed comparisons. The
checker verifies readiness/version before and after inference, response version
stamp, greedy text/token strings, exact top-five membership, finite logprobs
and the fixed threshold.
