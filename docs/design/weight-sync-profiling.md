# Weight synchronization profiling

Set `CHALK_WEIGHT_SYNC_PROFILE=1` in the serving group environment to emit JSON log records whose `event` is `chalk_weight_sync_profile`. Profiling is disabled by default. It does not change admission, application order, native cache reset, or the existing final device synchronization. It prepares no work in the background and claims no additional compute.

The real Qwen3-8B/A100 training integration observed admission pauses of 45.688 and 21.869 seconds for 4.651 and 2.503 GB publications. Those measurements materially miss the sub-second goal. They predate this instrumentation and cannot be attributed to individual stages. This profiling change has CPU regression coverage; its CUDA timings have not yet been measured on a real GPU.

There are three record phases:

- `sync`: source/target versions, mount reload and supervisor payload verification before admission closes, drain wait, and the unchanged total apply/admission-pause measurements. Apply includes native collective RPCs and cache reset. These existing totals remain the latency acceptance measurements.
- `apply`: publication version, collective RPC host duration, and one validated worker profile for each distinct global rank. Each worker includes global/local rank, PID, device, fixed phase names, finite nonnegative durations, and byte/tensor/batch counts. Invalid or missing worker telemetry fails the update closed because device mutation may already have occurred.
- `cache_reset`: engine cache-reset HTTP round-trip host duration. This is an engine control measurement, not per-worker device time.

Worker host phases use local monotonic elapsed time:

| Field | Included work |
| --- | --- |
| `manifest` | Read and parse the worker manifest |
| `verify` | Independent worker verification of all payloads before device writes |
| `read_checksum` | Read each patch segment and validate its checksum |
| `decode` | Decode/checksum, zstd decompression, CPU copies, sparse delta validation and int64 index expansion |
| `h2d_host` | Explicit sparse values/indices transfer and checkpoint-patch object construction; includes any host wait in the existing blocking `.to(device)` calls |
| `dense_load_host` | Native dense `model.load_weights`, including its internal mapping/transfers; keyframe lazy file reads are included here |
| `sparse_load_host` | Native `load_checkpoint_weight_patches`, including mapping, NaN expansion, destination writes and its existing internal synchronization |
| `synchronize_host` | Host wait in the existing single final device synchronization |

`stream_elapsed_ms` contains CUDA event intervals for explicit sparse H2D and native dense/sparse load regions. Events use the worker's current stream on its actual device. They are read only after the existing final device synchronization; no per-tensor synchronization is added. CPU devices produce an empty event map. These are elapsed intervals on one stream, not active GPU kernel time. Host enqueue gaps, stream backlog and overlap may contribute; unjoined auxiliary streams are not covered. Do not add worker, host and device measurements together as wall time. Event instrumentation itself adds overhead when enabled.

The native helper does not expose separate name-mapping, internal H2D, NaN expansion and destination-write timings, so this implementation does not invent those attributions. The vLLM 0.33 sparse helper expands a patch into full-tensor work and temporarily patches `torch.Tensor.copy_`; it must remain inside the drained admission gate. Synchronous `collective_rpc` work also occupies the native engine scheduler even if an external admission timer were moved.

A future preparation design needs immutable manifest/payload identity, explicit CPU/GPU memory limits and cancellation/ownership, plus evidence that a quick kickoff/poll RPC actually returns while generation proceeds. Long worker preparation inside a synchronous collective RPC cannot be described as nonblocking. Profiling attribution and a separately reviewed bounded experiment should precede that change.
