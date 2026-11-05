# Synthetic streaming fixture assessment

The retained fixture architecture holds a whole target, a publisher-shaped shadow,
and an independent full reconstruction; it writes a combined target and a combined
reconstruction. The streaming prototype reads one source shard at a time, writes
one target shard, and independently verifies persisted patches without retaining
a full reconstruction. This measures the retention shape, not the network SDK
publisher, remote upload/cache behavior, or an inference engine.

## Measured local result

One run on the local macOS host on 2026-11-05 used 32 generated BF16 tensors,
1,048,576 elements each, four tensors per source shard: **67,108,864 bytes** of
synthetic source data. No real model, image, remote compute, or remote storage
was accessed. Each child had a 120-second wall-clock timeout. Total elapsed time
including generation and imports was approximately 10.14 seconds.

| Measurement | Retained | Streaming |
|---|---:|---:|
| Whole-child peak RSS, including Python/Torch | 491,962,368 B | 236,306,432 B |
| Peak logical input/output file bytes | 201,884,029 B | 134,772,229 B |
| Peak allocated input/output file bytes | 201,924,608 B | 134,840,320 B |
| Build and independent proof time, excluding imports | 3.309 s | 2.157 s |
| Encoded patch bytes | 534,765 B | 534,765 B |

Both paths proved exactly 335,544 changed elements out of 33,554,432,
`floor(total / 100)`, using largest-remainder allocation across tensors. Trainer
target hashes, decoded persisted SDK patches, and independently reopened target
safetensors agreed. A separate scalar mantissa-XOR oracle checked every decoded
tensor's storage bytes and actual changed count, independently of the mutator and
diff encoder. Both paths produced canonical tensor hash catalog SHA-256
`b221d3966920544581888f8a081ae4aa4830459d97c1997d4fc0733a44045f7f`.

The parent deletes exact-owned retained outputs before running streaming and
removes the complete temporary directory on success or exception. The cleanup
ledger confirms removal for this run. Detailed local evidence is in
`chalk-private/.artifacts/weight-sync-p0/streaming-smallfixture-v1/`.

Validation: all 151 weight-sync harness tests passed, including seven focused
synthetic tests covering oversized input rejection, allocated block accounting,
corrupt reference rejection, independent oracle disagreement, and timeout cleanup.
The three added Python files pass Ruff and ty.

## Reproduce safely

From this SDK checkout, use an existing Python environment with Torch,
safetensors, and the SDK dependencies already available:

```sh
python scripts/weight_sync/profile_streaming_benchmark.py \
  --out /absolute/new/artifact/directory \
  --sdk-root /absolute/sdk/source/root \
  --timeout 120
```

The runner generates its own input in an owned temporary directory. The source
size check reserves 32 MiB for patches, headers, and filesystem block rounding
above three dense copies; inputs are limited to 1,024 tensors. Logical and
allocated file usage is sampled at controlled write boundaries and capped at
256 MiB for the complete owned fixture tree. These are observed file peaks,
not a filesystem-wide continuous trace. RSS comes from a fresh subprocess's
`getrusage(RUSAGE_SELF).ru_maxrss`, with platform units converted to bytes.
Failure leaves only a small cleanup ledger in the requested artifact directory;
successful runs also retain the JSON measurement report.

## Limits and next implementation gates

These small synthetic results do **not** measure full-8B peaks or authorize a
lower resource gate. The frozen v4 profiling package, its 120 GiB host-free-disk
requirement, and its separate CPU/GPU authorization remain unchanged. The
previously observed host disk capacity still prevents executing that package.

A production streaming fixture path must still validate actual HF shard and
largest-tensor distributions, the complete 399-tensor canonical mapping and native
fused mapping, grouped SDK uploads/cache retention, atomic manifest publication,
crash recovery, finite dtype handling, and actual guest quota/cgroup placement.
Full-size measurements and independently verified resource release are required
before changing the profiling resource assumptions. No full-size projection is
presented as measured here.
