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
`x-chalk-weight-version`; `/chalk/metrics` exposes Prometheus measurements.

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
