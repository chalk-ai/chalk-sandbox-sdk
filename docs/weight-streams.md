# Weight streams

Install `chalkcompute[weights]` in the trainer. A stream is a ChalkFS volume whose
`main` ref contains immutable `v000000/` checkpoint and patch directories, plus
`CURRENT` and an optional `PIN`. Mount `stream.volume` with `ref_name="main"` in
the serving runtime. A publication writes its payloads, manifest, and CURRENT in
one commit; it refuses more than 200 files before uploading anything.

```python
from chalkcompute.weights import WeightStream

stream = WeightStream("policy-qwen3", "Qwen/Qwen3-8B").create()
stream.seed_from_hf("Qwen/Qwen3-8B", revision="<immutable HF commit>")
publisher = stream.publisher(keyframe_every=50)
publisher.resume(model.state_dict())  # verifies exact equality to CURRENT
# Run an optimizer step, then synchronize training before each publication.
version = publisher.publish(model.state_dict(), step=training_step)
stream.pin(version)  # rollback is also a pin to an earlier retained version
stream.unpin()
stream.materialize(version, "/tmp/promoted-policy")
```

For a new trainer-owned checkpoint, use `stream.publisher(config=model_config)`
and call `publish(state_dict)` to create version zero. To preserve a pretrained
model's tokenizer and generation assets, seed from Hugging Face first; later
keyframes copy these assets from the earlier keyframe. Standard safetensors
weights and config are stored in every keyframe.

Publish from **one active process per stream**. The publisher keeps an independent
prior state on the tensors' devices and rejects stale parents; it does not provide
a distributed writer lease. An FSDP trainer must gather a complete state dict into
the writer: rank-local fragments are not a supported publication API. Pause or
synchronize optimizer mutation while publishing. Real full-parameter training can
change most weights; dense fallback is expected and sparse latency is not promised.

`resume` materializes the current checkpoint on CPU and compares every storage bit
with the caller's state before accepting it as the diff parent. It is deliberately
explicit and can be expensive for large models. `materialize` also uses host memory
for a full state dict and stages its output beside the destination, which must not
already exist.

Run `prune(keep_keyframes=2)` only while pin/retention administration is quiescent.
It preserves a keyframe and replay chain for a current pin. Pruning removes old
paths from the live ref; immutable historical ChalkFS snapshots remain and this
API does not claim storage reclamation.

## Reusable CPU-only FTQA smoke

From a source checkout with optional dependencies and the native volume extension:

```sh
python scripts/weight_stream_smoke.py --env-file /path/to/ftqa.env --output-dir /tmp/weight-stream-smoke
```

The script creates a unique volume, publishes a keyframe/patch/restore, exercises
pin/unpin and exact materialization, then deletes only its volume in `finally`
and verifies deletion. It allocates no sandbox or GPU. Credentials are never
included in its JSON result.

## Optional control-plane registration

The example above works directly with the volume. Call `stream.register()` to
attach an existing volume (including already published or live-ref-pruned history)
to WeightStreamService and index its publications. `stream.id` then exposes its
immutable control-plane identity. Registration checks that an existing stream name
has the same volume and base model; a missing base model can be recovered from a
published manifest. Registered pin/unpin operations use the service's authoritative
PIN commit and metadata path. The volume's authenticated environment is used.

After registration each publication first commits the volume, advances the
publisher's retained diff state, and then records its metadata. A metadata outage
raises `WeightMetadataPending`, which includes `version` and `volume_version_id`:
**the publication has already succeeded**. Continue with the next logical version;
retry `stream.reconcile()` to index committed publications rather than publishing
the same version again. For example:

```python
from chalkcompute.weights import WeightMetadataPending

stream.register()
try:
    version = publisher.publish(model.state_dict(), step=training_step)
except WeightMetadataPending as committed:
    version = committed.version
    stream.reconcile()  # Retry later if the control plane is still unavailable.
```

Manifests and immutable volume history form a durable retry outbox. Reconciliation
records missing versions newest first using each publication's original snapshot;
PIN writes, later publications, and live-ref pruning never substitute a later
snapshot into an existing descriptor. Repeated reconciliation is idempotent, also
when an indexing response was lost after server success. Normal publication uses
the batch commit's returned immutable version and does not scan history.

`reconcile(max_snapshots=1000)` traverses volume history lazily in bounded pages,
refuses incomplete or ambiguous history, and fails clearly if the explicit snapshot
bound is exceeded. Raise the bound deliberately for longer histories. Inspect
`stream.reconciliation_stats` for snapshot/read/record counts and elapsed seconds.
Dense fallback does not retain the exact changed-element count: indexed density is
the transmitted-element fraction, an upper bound on actual changed elements.
CURRENT/PIN in the volume remain authoritative even when the index is unavailable.
