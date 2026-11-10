# vLLM resident sparse updates

Select `--engine vllm-sparse` to apply indexed BF16 replacements to an existing
TP1 resident model. This opt-in bridge uses an explicit user-supplied tensor
layout; it does not select a model-specific mapping or invoke the full-weight
loader during updates. The ordinary `--engine vllm` path is unchanged.

Install the serving extra in the inference container. Normal `chalkcompute`
installation does not gain Torch or inference-engine dependencies.

Serialize a `WeightLayout` after calling
`layout.bind(WeightSchema.from_manifest(initial_keyframe_manifest))`. The file
can live outside the mutable published weight directory. Bind it to the complete
initial logical schema, including tensor names, shapes and dtypes. The resident
core compiles its mappings against the actual loaded parameters and verifies the
initial keyframe before opening admissions. See the shared sparse-weight guide
for a recipe and qualification workflow.

```sh
chalk-live-serve --engine vllm-sparse --weights /weights/model \
  --weight-layout /config/layout.json \
  --weight-layout-sha256 "$WEIGHT_LAYOUT_FILE_SHA256"
```

The adapter reads the bounded layout once before launch and sends those exact
bytes and their SHA256 to the worker at bind. Editing that pathname later cannot
change the active mapping. The CLI digest covers raw file bytes, including JSON
whitespace; ACK `layout_sha256` is the canonical `WeightLayout.sha256` and
`schema_sha256` identifies its logical source schema. Both are checked on bind
and every apply ACK, alongside the manifest, version and resident identity.

The bridge conservatively pins vLLM 0.33.0 and its generic RPC/cache/worker source
contracts, CUDA package versions, BF16, one CUDA device and TP/PP/DP1. It disables
prefix caching, compilation/CUDA graphs and speculative decoding. Only bounded
memory/context arguments are accepted. These are admission constraints, not a
claim that every model or GPU satisfying them is qualified. A new model/layout
still needs native initial-byte, indexed-write, cache/drain and fresh-native
output verification. Quantized and multi-rank deployments are not supported.

Batch invariance is optional: the bridge does not set `VLLM_BATCH_INVARIANT`.
For verification against a freshly loaded reference, explicitly set
`VLLM_BATCH_INVARIANT=1` in both serving environments to reduce numerical
differences caused by batching. Leaving it unset uses vLLM's default; an explicit
`0` is also accepted. Sparse indexed writes do not require batch-invariant
kernels. Historical output comparisons used the enabled setting and do not
qualify the default setting or additional attention backends.

The bridge sets `VLLM_SERVER_DEV_MODE=1` because its current implementation uses
vLLM's development-mode `/collective_rpc` and cache-reset endpoints. This is a
control-interface dependency, not a property of sparse patches. The native vLLM
server binds only to loopback behind the supervisor; do not expose those control
endpoints publicly. The bridge also sets `VLLM_WORKER_MULTIPROC_METHOD=spawn` so
CUDA workers start in fresh processes instead of inheriting parent state through
`fork`. Neither setting enables batch invariance.

The supervisor drains active requests before applying forward sparse patches,
resets the native cache before reopening admissions and advances its served
version after acknowledgment. Dense publications, incompatible keyframes or
backward pins require restarting from a keyframe. A higher-version forward sparse
publication can restore earlier values without moving the served version backward. Uncertain ACKs, cancellation,
identity changes and cache failures keep the replica unhealthy; restart rather
than continuing from an uncertain state. Exact successful receipt replay does
not repeat resident writes; conflicting reuse of a version is rejected.
