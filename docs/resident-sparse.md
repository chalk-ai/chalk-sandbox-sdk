# Resident sparse weight updates

`ResidentWeights` applies absolute sparse BF16 values to a caller-loaded model.
The checkpoint's tensor names and dimensions come from its complete initial
manifest. A user-supplied `WeightLayout` describes the relationship between those
logical tensors and the resident parameters. The SDK contains no model-specific
name, dimension, layer-count or model-ID table.

```python
from chalkcompute.weights import WeightLayout, WeightSchema
from chalkcompute.weights.serving.resident import ResidentWeights

# initial is the complete WeightManifest; model is already loaded and drained.
schema = WeightSchema.from_manifest(initial)
layout = WeightLayout(match_by_name=True).bind(schema)
resident = ResidentWeights(model, config_bytes, initial, layout)
bind_ack = resident.verify_initial(initial_directory)
ack = resident.apply(patch_directory, expected_version, manifest_sha256)
```

Matching by name works only when the complete logical and resident catalogs have
the same names, shapes and dtypes. For packed or transformed resident tensors,
provide explicit layout rules. Layout compilation must account for every source
and target exactly once; undeclared parameters, ambiguous rules, wrong shapes
and wrong dtypes are refused. The layout's source-schema digest must already
match the initial manifest. Bind and apply acknowledgments include both
`schema_sha256` and `layout_sha256`, and both are part of the resident identity.
The existing manifest `architecture_hash` retains its meaning: canonical JSON
configuration plus logical tensor names, shapes and dtypes. A patch must also
retain the initial manifest's `base_model` identity.

The initial audit checks every logical tensor against its complete safetensors
checkpoint, including byte equality and signed zero. It uses the layout's dense
inverse, independently of the sparse coordinate mapper. It does not call a model
loader. This full startup read and device-to-CPU audit are excluded from update
timings. The current inverse audit may copy a packed resident parameter to CPU
once for each logical source mapped into it; this startup overhead is not
optimized away or included in delta measurements. Initial payload size is bounded by declared BF16 model bytes plus a
16MiB safetensors-header reserve. Config and manifest reads are each bounded to
1MiB and reject duplicate JSON fields and nonfinite values.

Later updates accept sparse patches only. Each publication may change at most
`total_model_elements // 50` elements (2%, rounded down), and compressed payload
files may total at most 512MiB. These are admission limits, not estimates of a
real optimizer's sparsity. All files, checksums, decoded values, indices and
layout coordinates are validated before the first resident write. Writes use
bounded chunks of `index_copy_`; a dense checkpoint loader is never a fallback.
Coordinate mapping uses vectorized CPU tensor operations, with temporary memory
proportional to changed elements and tensor rank, rather than per-element Python
loops. Provision host memory for complete payload prevalidation and mapping.
Keyframes and dense patches require a suitable complete checkpoint and restart.

Updates require the immediately applied parent version. A content-identical
retry returns an acknowledgment without another write; conflicting content at
the current version is refused. An empty patch can advance the version without
synchronizing the device. Version and manifest digest are committed together
only after the update and acknowledgment construction succeed. Any uncertainty
during device synchronization or mutation, or a changed process/model/parameter/
storage identity, permanently poisons the resident and requires restart.

This implementation restricts resident allocations to contiguous, whole,
unaliased BF16 parameters on one NVIDIA CUDA device. Tied allocations, quantized
parameters, distributed/sharded replicas and derived-weight caches are not
qualified by this interface. The runtime capability check requires native BF16
support; it does not advertise qualification on additional GPU models. Engine
versions, dependency versions and reviewed integration sources remain pinned.
New engines, layouts and devices need their own correctness qualification.

The caller or engine adapter owns request draining, cache reset, runtime health
and version exposure. Updating parameters behind a running engine without those
barriers is unsupported. CPU tests verify layout, codec, indexed-write and
failure policy; they do not qualify a production GPU/HTTP serving integration.

Torch, serving engines and volume clients remain lazy optional imports. Install
the serving extra only in code that constructs or uses a resident updater; the
default container/sandbox SDK has no new dependency.
