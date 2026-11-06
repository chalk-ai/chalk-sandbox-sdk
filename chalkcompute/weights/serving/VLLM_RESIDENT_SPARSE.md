# Experimental Kimi direct resident scatter

`WeightSyncExtension` exposes four opt-in internal RPCs for a narrow Kimi-K3
correctness experiment. The existing `ws_apply_dir` serving path is unchanged.
The experiment must use these new RPCs explicitly; no production syncer or runtime
catalog entry selects them automatically.

The supported profile is `kimi-k3-bf16-tp8-v1`: vLLM 0.33.0's AMD
`KimiK3ForConditionalGeneration` on gfx950, TP8/PP1/DP1. It accepts only:

| Resident parameter | Global shape | Local mapping |
| --- | --- | --- |
| `language_model.model.norm.weight` | 7168 | Replicated, identical flat indices on every rank |
| `language_model.lm_head.weight` | 163840 × 7168 | Contiguous vocabulary rows 20480×7168 per TP rank |

Both parameters must be plain BF16 Parameters with contiguous, exclusive storage.
The head must use `UnquantizedEmbeddingMethod` with exactly the expected actual
vocabulary shard indices, no padding or added vocabulary. Packed experts, scales,
fused projections, custom layouts and other names are rejected. Coordinates are
explicit resident/global tensor coordinates; this does not implement arbitrary
checkpoint-name mapping, trainer requantization or quantized expert updates.

The vLLM 0.33 `checkpoint_weight_patch` helper builds full checkpoint-shaped NaN
staging tensors and performs full-shaped masked writes through `model.load_weights`.
The new commit path instead uses only `parameter.view(-1).index_copy_(...)` on
resident storage, followed by synchronization. Staging, device transfer and writes
scale with changed elements; no full parameter staging/load or NaN mask is used.

Callers must keep native controls on loopback and close admission/drain all native
work before the following sequence:

1. `ws_sparse_probe()` checks the runtime/model/layout and reports rank, PID/start,
   device, model/parameter identities, storage pointers and shapes. Initial stream
   version is 0, representing the caller's independently verified loaded checkpoint.
2. `ws_sparse_prepare_dir(directory, expected_version, transaction_id)` reads a
   verified patch manifest and sparse codec payloads. The manifest must name
   `moonshotai/Kimi-K3`, parent=current and positive target version, contain one or
   two supported sparse BF16 entries and at most 4096 total changed elements.
   Values must be finite; indices sorted, unique and in bounds. Manifest/payload
   sizes are bounded. A transaction ID is 32 lowercase hex characters.
3. Collect and validate all eight distinct prepared rank ACKs, identical manifest
   and architecture hashes, expected mapped indices/counts and identities matching
   the loaded baseline. Build `{str(rank): identity_sha256}` for ranks 0 through 7.
4. `ws_sparse_commit(transaction_id, expected_version, rank_identities, manifest_sha256)`
   rechecks local model/storage identity and current parent before writing only
   staged changed values. Exact replay of the last committed transaction returns
   its previous ACK without writing. A different version or payload hash is rejected.
5. Verify all eight committed ACKs and reset applicable prefix/KV/hybrid caches
   before reopening admission. Any missing/uncertain commit or cache reset requires
   terminating the entire replica. The local failed-mutation flag cannot be cleared
   by abort; partial distributed updates have no rollback guarantee.

`ws_sparse_abort(transaction_id)` discards matching prepared staging. Preparation
does not mutate resident weights, so a failed prepare can be aborted on other ranks.
Two-phase preparation is not a distributed atomic transaction. The caller owns
admission, all-rank validation, cache invalidation and fail-closed shutdown. Restore
uses a new higher version with parent=current and original absolute sparse values.

The initial manifest architecture hash is an externally bound stream identity;
later updates must retain it. The experiment must independently verify immutable
model revision, actual config/schema hash, image digest, installed native source
hashes and effective settings before preparing. This API does not infer a full
checkpoint architecture hash from the resident quantized model.

CPU tests use real sparse encoding and a reduced eight-rank fixture, checking
unchanged elements/pointers, shard boundaries, indexed-only commit dispatch,
prevalidation, replay, stale identity and poisoned mutation handling. Metadata
doubles check unsupported native topology/layout gates. They do not prove actual
Kimi or AMD execution. Native validation and any production integration remain
pending a separately frozen experiment with independent resource release.
