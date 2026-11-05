Add `chalkcompute[serving]` and `chalk-live-serve` for following Chalk weight
streams through a streaming OpenAI proxy and vLLM 0.33 worker extension.
Updates verify payloads, drain active requests, apply absolute weights, reset
prefix caches, and stamp responses with `x-chalk-policy-version`. Rollback
reconstructs the desired version from a compatible keyframe. Failed device
writes and uncertain request cancellation make replicas unhealthy and stop
their engine process groups.

Configuration: `CHALK_ENGINE`, `CHALK_WEIGHT_STREAM_DIR`, `CHALK_LISTEN_ADDR`,
`CHALK_WEIGHT_SYNC_POLL_S`, `CHALK_WEIGHT_SYNC_DRAIN_TIMEOUT_S`, and
`CHALK_WEIGHT_SYNC_KEYFRAME_LAG`. Health exposes `x-chalk-weight-version`;
`/chalk/metrics` exports apply/pause time, payload bytes/density, and version lag.
