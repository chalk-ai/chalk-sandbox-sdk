# Live weight serving

Install the SDK's `serving` extra into a **vLLM 0.33** image. The SDK does not
install vLLM: CUDA and ROCm runtimes need different engine distributions.

```sh
chalk-live-serve --engine vllm --weights /chalk/weights -- --tensor-parallel-size 1
```

Mount the weight stream volume with `ref_name="main"`. The runtime refreshes
ChalkFS via `.chalk-volume/ctl` before reading `PIN` or `CURRENT`. A local
directory also works without a ChalkFS control file.

Only the supervisor port is public. The native engine binds `127.0.0.1:8001`;
its development-only weight RPC/cache endpoints must never be exposed. The
proxy admits known native OpenAI API routes and rejects management routes.
Streaming bodies pass through without buffering. Each admitted response carries
`x-chalk-policy-version`. `/chalk/health` returns the applied version and
`x-chalk-weight-version`; `/chalk/metrics` exposes Prometheus measurements. Density is the final target
publication's transmitted elements divided by the complete keyframe's model element
count. Sparse entries count exact changes; dense entries count the entire tensor,
so this fraction is an upper bound on the true changed-element fraction.
Apply time and bytes include every version in that replay batch; shared
safetensors shard bytes are counted once per version.

The supervisor verifies every payload in the planned replay before any device
write. It holds admissions, waits for existing responses to finish, applies the
plan through worker file reads, resets prefix caches, advances the policy
version, and resumes admissions. A drain timeout abandons the update and keeps
the old policy. It cannot safely infer that disconnecting a native HTTP request
has stopped GPU decoding. Client cancellation or uncertain transport failure
therefore marks the replica unhealthy and restarts its engine process group.
Any failed device write/cache reset does the same.

Boot and backward PIN changes load a complete keyframe at or before the desired
version, then replay its parent chain. A newer keyframe cannot restore an older
PIN. Missing/corrupt publications and architecture changes are refused while
the old healthy policy continues serving. If the stream prunes every keyframe
capable of materializing a PIN, restore a retained keyframe before retrying.

## Local managed-target integration

An opt-in local/test target-file adapter is available with
`--managed-target-file PATH` or `CHALK_WEIGHT_TARGET_FILE`. It requires these
expected identities from the launcher:

| Variable | Target field |
| --- | --- |
| `CHALK_MODEL_DEPLOYMENT_ID` | `deployment_id` |
| `CHALK_MODEL_DEPLOYMENT_REVISION_ID` | `revision_id` |
| `CHALK_WEIGHT_STREAM_ID` | `stream_id` |
| `CHALK_WEIGHT_VOLUME_ID` | `volume_id` |

The JSON descriptor has `schema_version: 1` and also supplies
`target_generation`, `logical_version`, `volume_version_id`,
`manifest_sha256`, `weight_schema_sha256`, and a UTC RFC3339
`lease_expires_at`. Target generations start at 1 and increase monotonically.
The file writer must replace the descriptor atomically. Managed mode never
falls back to `PIN` or `CURRENT`. It checks the expected identities, manifest
and schema digests, and every payload in the selected replay before device
writes. Equal-generation identity/content drift, missing or malformed delivery,
and an expired lease fail the replica closed. A new generation closes readiness
and request admission while the target is planned and applied. The health JSON
adds `applied_target` only after the selected content is verified and installed.

The lease is limited to 300 seconds to match the native serving handle's bound.
Renewal must deliver a later absolute expiry; re-reading the same descriptor
cannot extend the native monotonic deadline. `volume_version_id` is recorded in
the desired/applied tuple as supplied publication provenance. This file adapter
does not attest which immutable volume snapshot backs the local mount.

This is a local integration primitive, not trusted controller delivery. No
controller transport, signature/authentication, or immutable mount binding is
included. Do not use it to activate a production model deployment until those
pieces and a pinned serving image are connected and qualified. The default
legacy `PIN`/`CURRENT` mode remains unchanged.

Managed targets are unsupported by the experimental full-checkpoint engine;
both the CLI and direct `FullCheckpointSyncer` construction reject that
combination.

Configuration:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CHALK_ENGINE` | `vllm` | Engine adapter (vLLM initially) |
| `CHALK_WEIGHT_STREAM_DIR` | required | Mounted stream root |
| `CHALK_LISTEN_ADDR` | `:8000` | Public supervisor address |
| `CHALK_WEIGHT_SYNC_POLL_S` | `2` | Ref refresh interval |
| `CHALK_WEIGHT_SYNC_DRAIN_TIMEOUT_S` | `30` | Maximum drain wait before abandoning update |
| `CHALK_WEIGHT_SYNC_KEYFRAME_LAG` | `50` | Prefer a complete keyframe beyond this version lag |

`--engine-port` and `--startup-timeout` bound internal startup configuration.
SIGTERM/SIGINT stop the entire engine process group. A failed supervisor exits
nonzero; the enclosing sandbox/scaling group should restart it.

Run focused CPU checks with `scripts/check_weight_serving.sh`; no GPU or cloud
allocation is needed. GPU validation must additionally confirm the installed
vLLM version, every rank acknowledgement, publish/restore output equivalence,
and actual pause/publish latency. CPU tests do not establish GPU performance
or quantized checkpoint support.
