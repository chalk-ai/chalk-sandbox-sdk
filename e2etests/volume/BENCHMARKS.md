# Volume I/O benchmark suite

This suite establishes repeatable baselines for mounted Chalk volumes. It
contains filesystem microbenchmarks, synthetic model loads, and synthetic
Hugging Face model reconstruction writes. The results are observational and
are not performance release gates. Buildkite sends a notification only when a
benchmark job itself fails.

## Layout

```text
e2etests/volume/
  BENCHMARKS.md
  benchmarks/
    fio.py
    model_load.py
    model_download.py
    payloads/
      fio.sh
      model_load.sh
      model_download.py
  grafana/
    volume-io-benchmarks.json
```

The `benchmarks/*.py` controllers own remote sandbox and volume lifecycles plus
pyperf output. The `payloads/` programs are copied into and run inside those
sandboxes. Buildkite arguments and suite names live in
`.buildkite/run-volume-benchmark.sh` so the pipeline YAML only describes
scheduling, the environment/benchmark matrix, and shared policy.

## Inventory

| Case | Run name | Workload | Block or extent | Jobs | Direct I/O | Cache state |
| --- | --- | --- | --- | ---: | --- | --- |
| `volume-fio` | `sequential-write` | fio microbenchmark | 4 MiB | configurable | configurable | n/a |
| `volume-fio` | `sequential-read` | fio microbenchmark | 4 MiB | configurable | configurable | unspecified |
| `volume-fio` | `random-read-4k` | fio microbenchmark | 4 KiB | configurable | configurable | unspecified |
| `volume-fio` | `random-write-4k` | fio microbenchmark | 4 KiB | configurable | configurable | n/a |
| `volume-fio` | `small-file-write` | fio microbenchmark | configurable | 1 | configurable | n/a |
| `volume-fio` | `small-file-read` | fio microbenchmark | configurable | 1 | configurable | unspecified |
| `volume-fio` | `volume-commit` | commit latency | n/a | n/a | n/a | n/a |
| `volume-fio` | `volume-results-ready` | snapshot visibility | n/a | n/a | n/a | n/a |
| `volume-model-load` | `fresh-read-jobs-1` | synthetic model load | 16 MiB | 1 | false | fresh sandbox |
| `volume-model-load` | `warm-read-jobs-1` | synthetic model load | 16 MiB | 1 | false | warm |
| `volume-model-load` | `fresh-read-jobs-4` | synthetic model load | 16 MiB | 4 | false | fresh sandbox |
| `volume-model-load` | `warm-read-jobs-4` | synthetic model load | 16 MiB | 4 | false | warm |
| `volume-model-download` | `gemma-shaped-direct-write` | synthetic model download | 4 MiB | 2 | n/a | n/a |
| `volume-model-download` | `gemma-shaped-direct-commit` | synthetic model download | 4 MiB | 2 | n/a | n/a |
| `volume-model-download` | `gemma-shaped-sequential-write` | synthetic model download | 4 MiB | 2 | n/a | n/a |
| `volume-model-download` | `gemma-shaped-sequential-commit` | synthetic model download | 4 MiB | 2 | n/a | n/a |

The model-download workload preserves the approximate 29:1 shard-size ratio of
Gemma 4 26B A4B at a smaller 1024 MiB plus 35 MiB scale. Direct mode writes
unique extents in a deterministic shuffled order. Sequential mode represents
Xet sequential reconstruction. Both modes write two shards concurrently and
report reconstruction and final commit separately.

## Workload design

### Fio microbenchmarks

The general suite covers sequential and random large-file I/O, small-file I/O,
commit latency, and the delay until a coherent committed result is readable.
It is a broad filesystem baseline rather than an application trace.

### Synthetic model load

The preparation sandbox writes four non-sparse 256 MiB shards by default and
commits them. Each configured concurrency level then runs in a new sandbox
against that immutable version. Readers use buffered `pread`, sequential
16 MiB application reads, and one shard per fio job.

Each reader records a `fresh-sandbox` pass followed immediately by a warm pass.
Fresh sandbox is deliberately not called cold: a new process and mount do not
prove that every host-side or backend cache is empty.

### Synthetic model download

The model-download worker isolates mount reconstruction from Hugging Face
network and model-runtime dependencies. It writes unique extents so
content-addressed storage cannot collapse the synthetic shard into one repeated
chunk. Direct mode deterministically shuffles extent offsets within each shard;
sequential mode writes the same extents in order. The two shard writers run
concurrently in both modes.

This profiles the volume pressure created while Xet reconstructs a Gemma-shaped
download, not Hugging Face network throughput or Xet CPU cost. Extent order can
later be replaced with a trace captured from a live download without changing
the benchmark result contract.

## Canonical naming

Buildkite uploads each module with these identifiers:

| Suite name | Case name |
| --- | --- |
| `Sandbox Volume FIO Benchmarks ({env})` | `volume-fio` |
| `Sandbox Volume Model Load Benchmarks ({env})` | `volume-model-load` |
| `Sandbox Volume Model Download Benchmarks ({env})` | `volume-model-download` |

`{env}` is the Buildkite matrix value, currently `staging/ftqa` or `meta-ci`.
The run name is the pyperf benchmark `metadata.name` value listed above.

## Canonical metadata

Pyperf metadata is uploaded to the `benchmark_results.metrics` JSONB column.
The benchmark runner merges metadata from the suite, benchmark, and run levels.
More specific levels override keys from earlier levels.

Common keys:

| Key | Type | Meaning |
| --- | --- | --- |
| `workload` | string | `fio-microbench`, `synthetic-model-load`, or `synthetic-model-download` |
| `sandbox_cpu` | string | Requested sandbox CPU |
| `sandbox_memory` | string | Requested sandbox memory |
| `volume_version_id` | integer | Relevant committed volume version when present |
| `benchmark_error_count` | integer | `0` for success or `1` for a recorded non-fatal benchmark error |
| `request_type` | string | Benchmark case that made the failed request |
| `error_type` | string | Stable error classification |

Fio and model-load keys:

| Key | Type | Meaning |
| --- | --- | --- |
| `fio_direction` | string | `read` or `write` |
| `fio_jobs` | integer | Jobs represented by one run |
| `fio_jobs_list` | string | Configured model-load job counts, such as `1,4` |
| `fio_size` | string | Fio file size per job |
| `fio_block_size` | string | Model-load application read size |
| `fio_direct` | integer | `0` for buffered or `1` for direct I/O |
| `fio_bw_bytes_per_sec` | number | Aggregate fio bandwidth |
| `fio_iops` | number | Aggregate fio operations per second |
| `fio_clat_mean_ns` | number | Mean completion latency |
| `fio_clat_p50_ns` | number | p50 completion latency when emitted by fio |
| `fio_clat_p95_ns` | number | p95 completion latency when emitted by fio |
| `fio_clat_p99_ns` | number | p99 completion latency when emitted by fio |
| `cache_state` | string | `fresh-sandbox` or `warm` for model loads |
| `model_volume_version_id` | integer | Immutable source version for model loads |

Synthetic model-download keys:

| Key | Type | Meaning |
| --- | --- | --- |
| `reconstruction_mode` | string | `direct` or `sequential` |
| `io_direction` | string | `write` |
| `io_jobs` | integer | Concurrent shard writers |
| `io_bytes` | integer | Total reconstructed bytes |
| `io_extent_bytes` | integer | Application write extent |
| `io_bw_bytes_per_sec` | number | Reconstruction bandwidth |
| `large_shard_bytes` | integer | Scaled large shard size |
| `small_shard_bytes` | integer | Scaled small shard size |

Keys are optional when they do not apply to a run. Historical rows from before
normalization may not contain `workload`, `fio_jobs_list`, or integer
`fio_jobs`.

## Data flow

```text
benchmark module
  -> CHALK_BENCHMARK_PYPERF_OUTPUT
  -> .buildkite/run-benchmark.sh
  -> benchmark-runner
  -> benchmark-results-server
  -> Postgres benchmark_results
  -> Grafana
```

The database row identifies the suite, case, run, branch, source, and timing
statistics. Additional pyperf metadata is available in `metrics`. When sandbox
creation fails because all registered host-pool capacity is allocated, the
benchmark wrapper writes a `benchmark-status` run with `benchmark_error_count=1`
and exits successfully. Successful attempts write the same run with
`benchmark_error_count=0`. Exhausted volume write spools are handled the same
way for reporting with `error_type=volume-spool-exhausted`, but remain hard
benchmark failures. Other errors also remain benchmark failures.

## Running

Set up local credentials and choose an output file:

```sh
just env

CHALK_BENCHMARK_PYPERF_OUTPUT=/tmp/fio.json \
  uv run python -m e2etests.volume.benchmarks.fio \
    --size 64M --small-files 100 --timeout-secs 900

CHALK_BENCHMARK_PYPERF_OUTPUT=/tmp/model-load.json \
  uv run python -m e2etests.volume.benchmarks.model_load

CHALK_BENCHMARK_PYPERF_OUTPUT=/tmp/model-download.json \
  uv run python -m e2etests.volume.benchmarks.model_download
```

Buildkite expands one two-dimensional matrix step into every case for
`staging/ftqa` and `meta-ci`. `.buildkite/run-volume-benchmark.sh` maps each
matrix workload to its module, arguments, suite, case, and tags. The pipeline
runs daily at 6am America/Los_Angeles on `main` and can also be launched
manually. Any hard failure on `main` notifies `#proj-compute-volumes`.

## Dashboard

Import [`grafana/volume-io-benchmarks.json`](grafana/volume-io-benchmarks.json)
into Grafana.

## Roadmap

- Multi-sandbox concurrent readers and writers
- Cold-mount time to first byte
- Large-file random reads
- Commit throughput by dirty-set size
- Trace-derived reconstruction extent ordering from a live Xet download

FUSE and object-store telemetry remains out of scope for the pyperf schema.
Correlate it externally by benchmark timestamp. Useful external measurements
include FUSE request sizes and latency, in-flight requests, object operation
concurrency, staged bytes, and cache hits.
