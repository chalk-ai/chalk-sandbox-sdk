# SGLang resident sparse updates

`--engine sglang-sparse` uses the generic, explicitly source-bound `WeightLayout`
contract described in [the vLLM bridge guide](resident-vllm-sparse.md). The ordinary
`sglang-experimental` full-checkpoint path remains separate and unchanged.

```sh
chalk-live-serve --engine sglang-sparse --weights /weights/model \
  --weight-layout /config/layout.json \
  --weight-layout-sha256 "$WEIGHT_LAYOUT_FILE_SHA256"
```

The adapter snapshots the layout bytes before launch. A private loopback route
and serialized native scheduler IPC channel deliver the exact bytes/hash at
bind; subsequent applications use that compiled resident mapping. Every ACK
binds canonical layout SHA, source schema SHA, manifest SHA, weight version and
native identity. The layout need not be stored inside any weight version.

Admission pins SGLang 0.5.23 and its generic scheduler/HTTP/IPC source contracts,
CUDA package versions, BF16 and one CUDA device with TP/PP/DP/EP1. The conservative
profile selects deterministic Triton attention and disables radix cache, CUDA
graphs, overlap scheduling, weight cache and speculative decoding. Only bounded
memory/context tuning is accepted. Runtime admission does not qualify arbitrary
models, MoE, quantization or GPU hardware; each model/layout needs native tests.

After supervisor drain, writes and native cache flush run in the scheduler's
serialized control call. A non-idle scheduler refuses the operation. Failed or
uncertain IPC permanently poisons the channel; the adapter/supervisor keeps
admissions closed until restart. Exact successful receipt replay is idempotent;
conflicting versions and incompatible publications fail closed. A backward pin
requires restart; a higher-version sparse publication can restore earlier values.

CPU integration tests use a custom two-tensor encoder packed into one resident
parameter, real safetensors/codec/layout/core operations, and CPU-only admission
seams. They prove bridge contracts, not GPU or production-serving performance.
