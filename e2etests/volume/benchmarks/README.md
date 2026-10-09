# Volume I/O benchmark suite

This suite establishes repeatable baselines for mounted Chalk volumes. It
contains filesystem microbenchmarks, synthetic model loads, synthetic Hugging
Face model reconstruction writes, and real Git repository ingestion and status
workloads. The results are observational and are not performance release gates.
Buildkite sends a notification only when a benchmark job itself fails.

## Layout

The `cases/*.py` controllers own each workload and return pyperf documents to
`harness.py`. The harness owns the Buildkite output-file contract, sandbox
lifecycle helpers, and pyperf status reporting. The matching `payloads/`
programs are copied into and run inside sandboxes. Buildkite arguments and suite
names live in the local `justfile` so the pipeline YAML only describes
scheduling, the environment/benchmark matrix, and shared policy.

The harness also archives every completed pyperf document to the
`volume-bench-results` volume. Files use UTC timestamped paths under
`pyperf/YYYY/MM/DD/` so results from concurrent benchmark cases remain distinct.

## Result handoff

Sandbox payloads write workload-native JSON to their mounted volumes and commit
before exiting. Controllers retrieve results through `read_committed_json()`,
which waits for a readable version, pins that immutable snapshot, and reads all
requested files from the same version:

```python
committed = read_committed_json(
    results_volume,
    {
        "fresh": "jobs-1/fresh-read-jobs-1.json",
        "warm": "jobs-1/warm-read-jobs-1.json",
    },
)
print(committed.version_id, committed.value)
```

The case converts those native results into a pyperf document and returns it.
The harness then writes the local Buildkite output file and archives the same
document. This keeps volume commit and immutable retrieval as the Compute result
channel while keeping CI-specific output handling out of sandbox payloads.

## Adding a benchmark

A case owns only its image, sandbox specification, payload result schema, and
pyperf conversion. The harness owns generated volume names and cleanup, sandbox
startup and termination, completion checks, failed-workload log retrieval, and
result publication. A minimal controller has this shape:

```python
from typing import Any

from chalkcompute import Sandbox
from e2etests.volume.benchmarks.harness import (
    read_committed_json,
    run_benchmark,
    run_sandbox_workload,
    temporary_volume,
)


def main() -> dict[str, Any]:
    with temporary_volume("sandbox-example") as volume:
        run_sandbox_workload(
            Sandbox(
                image=build_image(),
                volumes=[volume.mount("/data")],
                entrypoint=["/bin/sh", "/opt/example/run.sh", "/data"],
            ),
            phase="example",
            timeout_secs=1800,
            log=(volume, "benchmark.log"),
        )
        result = read_committed_json(volume, {"result": "result.json"})
        return build_pyperf_results(result.value["result"])


if __name__ == "__main__":
    run_benchmark("volume-example", main)
```

`temporary_volume()` creates the volume on entry and deletes and closes it on
exit. Pass `keep=True` to retain a generated volume for debugging. Pass
`name=...` to use a caller-owned volume; named volumes are closed but never
deleted. Use `contextlib.ExitStack` when a case needs a dynamic set of volumes.

To register the case, add its payload under `payloads/<name>/`, its controller
under `cases/<name>.py`, one recipe to the benchmark `justfile`, and its recipe
name to the Buildkite matrix. The two CI entries stay explicit so Buildkite can
show the complete scheduled matrix before jobs run.

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
| `volume-git-repo` | `fuse-clone-write` | vLLM Git checkout to mount | n/a | n/a | n/a | n/a |
| `volume-git-repo` | `fuse-clone-commit` | vLLM checkout commit | n/a | n/a | n/a | n/a |
| `volume-git-repo` | `sdk-load-git-repo` | SDK Git checkout and upload | n/a | n/a | n/a | n/a |
| `volume-git-repo` | `local-git-clone` | local Git checkout | n/a | n/a | n/a | n/a |
| `volume-git-repo` | `git-status-on-fuse-clone` | Git work-tree traversal | n/a | n/a | n/a | unspecified |
| `volume-git-repo` | `git-status-on-sdk-clone` | Git work-tree traversal | n/a | n/a | n/a | unspecified |
| `volume-git-repo` | `git-commit-on-fuse-clone` | Git commit creation | n/a | n/a | n/a | n/a |
| `volume-git-repo` | `git-commit-on-sdk-clone` | Git commit creation | n/a | n/a | n/a | n/a |
| `volume-sqlite` | `sqlite-insert` | SQLite WAL bulk insert, `synchronous=FULL` | 50k-row transactions | 1 | false | n/a |
| `volume-sqlite` | `sqlite-read` | SQLite full scan and 10k point reads | n/a | 1 | false | warm |
| `volume-sqlite` | `sqlite-journal-delete` | 200 rollback journal transactions | n/a | 1 | false | n/a |
| `volume-sqlite` | `sqlite-vacuum-rename` | `VACUUM INTO` then rename over live DB | n/a | 1 | false | n/a |
| `volume-sqlite` | `sqlite-rename-cycles` | 50 small DB write then rename cycles | n/a | 1 | false | n/a |
| `volume-sqlite` | `sqlite-nested-dir-rename` | 50 nested folder renames above live DBs | n/a | 1 | false | n/a |
| `volume-sqlite` | `sqlite-integrity-check` | `PRAGMA integrity_check` | n/a | 1 | false | warm |
| `volume-sqlite` | `sqlite-verify-*` | integrity, nested DBs, WAL reopen after commit | n/a | 1 | false | fresh sandbox |
| `volume-sqlite` | `sqlite-{write,verify}-volume-commit` | commit latency | n/a | n/a | n/a | n/a |

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

### Git repository ingest, status, and commit

The Git workload checks out vLLM at a fixed revision into two volumes. One
checkout writes through a FUSE mount and commits the resulting tree. The other
uses `Volume.load_git_repo()`, which checks out locally and uploads through the
native client. A separate local checkout timing allows the SDK upload component
to be estimated from the combined `sdk-load-git-repo` value. The benchmark
allows `github.com` egress for the FUSE checkout, compares all checkout file
counts, and includes `.git` metadata in both volumes.

After ingestion, a new sandbox mounts each Git-ready volume and times one
`git -C /git-repo/vllm status --porcelain=v1 --untracked-files=all`. This
measures ordinary Git traversal and file comparison cost after each ingestion
path. The persisted repository metadata also supports subsequent branch,
commit, and push workflows when credentials and network policy permit them.

Each commit benchmark then appends a fixed marker to the tracked README outside
the timed section. It times one
`git commit -am "Benchmark mounted Git commit"`, including staging and Git's
object, ref, and index writes through the mount. Publishing those changes as a
Chalk volume version happens after the timed Git command. Both ingestion paths
run even if one commit fails, then any commit failure fails the benchmark after
the timings and exit codes are written. Author and committer identity are
provided through standard Git environment variables; signing behavior is not
overridden.

### SQLite

A customer reported SQLite corruption and failures around file renames on
volumes. One sandbox grows a SQLite database to 5 GiB in WAL mode with
`synchronous=FULL`, then runs reads, rollback journal transactions (journal
create and unlink per commit), an atomic `VACUUM INTO` plus rename over the
live database, repeated small database rename cycles, and repeated renames of
top level and intermediate folders above live databases. Every rename is
followed by reopening and checking the database. The volume is committed and a
fresh sandbox reruns `integrity_check`, checks the nested databases, and reopens
the main database in WAL mode. Any SQLite error fails the benchmark after the
completed phase timings are published.

## Canonical naming

Buildkite uploads each module with these identifiers:

| Suite name | Case name |
| --- | --- |
| `Sandbox Volume FIO Benchmarks ({env})` | `volume-fio` |
| `Sandbox Volume Model Load Benchmarks ({env})` | `volume-model-load` |
| `Sandbox Volume Model Download Benchmarks ({env})` | `volume-model-download` |
| `Sandbox Volume Git Repo Benchmarks ({env})` | `volume-git-repo` |
| `Sandbox Volume SQLite Benchmarks ({env})` | `volume-sqlite` |

`{env}` is the Buildkite matrix value, currently `staging/ftqa` or `meta-ci`.
The run name is the pyperf benchmark `metadata.name` value listed above.

## Canonical metadata

Pyperf metadata is uploaded to the `benchmark_results.metrics` JSONB column.
The benchmark runner merges metadata from the suite, benchmark, and run levels.
More specific levels override keys from earlier levels.

Common keys:

| Key | Type | Meaning |
| --- | --- | --- |
| `workload` | string | `fio-microbench`, `synthetic-model-load`, `synthetic-model-download`, `git-repo`, or `sqlite` |
| `sandbox_cpu` | string | Requested sandbox CPU |
| `sandbox_memory` | string | Requested sandbox memory |
| `volume_version_id` | integer | Relevant committed volume version when present |
| `benchmark_error_count` | integer | `0` for success or `1` for a recorded non-fatal benchmark error |
| `request_type` | string | Benchmark case that made the failed request |
| `error_type` | string | Stable error classification |
| `exception_class` | string | Python exception class for the recorded error |

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

Git repository keys:

| Key | Type | Meaning |
| --- | --- | --- |
| `repo_url` | string | Git remote URL |
| `repo_ref` | string | Pinned Git revision |
| `repo_file_count` | integer | Files in the checked-out repository |
| `clone_path` | string | `fuse` or `sdk` ingestion path |
| `git_command` | string | Timed Git command and arguments |
| `git_status_dirty_paths` | integer | Paths reported by `git status` |
| `git_commit_sha` | string | Commit created by the timed Git command |
| `git_exit_code` | integer | Exit code from the timed Git command |
| `volume_versions_created` | integer | Versions created by the SDK repository upload |

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
and exits successfully. The same classification covers failed creation when no
registered host has capacity for the requested resources. Other benchmark
exceptions are recorded with `error_type=unclassified` and their exception class,
then remain hard failures. Benchmark failures with partial timings are recorded
as `error_type=benchmark-failure`. Sandboxes preempted after starting are reported the
same way with `error_type=sandbox-preempted`. Sandboxes terminated because their
host became unavailable, including during maintenance windows, are reported with
`error_type=sandbox-host-unavailable` and exit successfully. Successful attempts
write the same run with `benchmark_error_count=0`. Exhausted volume write spools are reported
with `error_type=volume-spool-exhausted`, but remain hard benchmark failures.
Other errors also remain benchmark failures.

## Running

Set up local credentials and choose an output file:

```sh
just env

# Run the canonical CI configuration locally
just --justfile e2etests/volume/benchmarks/justfile fio staging/ftqa

CHALK_BENCHMARK_PYPERF_OUTPUT=/tmp/fio.json \
  uv run python -m e2etests.volume.benchmarks.cases.fio \
    --size 64M --small-files 100 --timeout-secs 900

CHALK_BENCHMARK_PYPERF_OUTPUT=/tmp/model-load.json \
  uv run python -m e2etests.volume.benchmarks.cases.model_load

CHALK_BENCHMARK_PYPERF_OUTPUT=/tmp/model-download.json \
  uv run python -m e2etests.volume.benchmarks.cases.model_download

CHALK_BENCHMARK_PYPERF_OUTPUT=/tmp/git-repo.json \
  uv run python -m e2etests.volume.benchmarks.cases.git_repo \
    --timeout-secs 1800
```

Buildkite expands one two-dimensional matrix step into every case for
`staging/ftqa` and `meta-ci`. The local `justfile` maps each matrix workload to
its module, arguments, suite, case, and tags. The pipeline
runs hourly on `main` and can also be launched manually. Any hard failure on
`main` notifies `#proj-compute-volumes`.

## Provider baselines

Compare the shared FIO workload across volume providers. Scheduled Buildkite
runs use Chalk only. Run other providers locally:

```sh
just --justfile e2etests/volume/benchmarks/justfile fio-run <candidate-provider> \
  --output benchmark-results/candidate.json

just --justfile e2etests/volume/benchmarks/justfile fio-run <baseline-provider> \
  --output benchmark-results/baseline.json

just --justfile e2etests/volume/benchmarks/justfile fio-compare \
  benchmark-results/candidate.json benchmark-results/baseline.json \
  --output benchmark-results/comparison.md
```

`fio-run` writes pyperf JSON and a sibling Markdown table locally. Without
`--output`, it uses a timestamped path in gitignored `benchmark-results/`.
Runs use temporary volumes; pass `--volume NAME` or `--keep-volume` to retain
one. Run `uv sync --all-groups` once, then configure credentials for the
provider you use.

Workload options are shared:

```sh
just --justfile e2etests/volume/benchmarks/justfile fio-run <provider> \
  --size 64M --small-files 100 --timeout-secs 900 \
  --provider-environment baseline --provider-region us-east
```

`--provider-environment`, `--provider-region`, and `--provider-cloud` apply
when the selected adapter supports placement. Requested values are recorded in
the result.

`fio-compare` accepts any two results. It requires matching workload settings,
resources, image, payload hash, FIO version, and transferred bytes. Ratios are
candidate / baseline: lower is better for elapsed time and latency; higher is
better for bandwidth and IOPS. Persistence timings remain in each individual
report.

To add a provider, register a runner in `cases.fio.providers.PROVIDERS` with
its dependency group and placement support.

## Dashboard

Import [`grafana/volume-io-benchmarks.json`](grafana/volume-io-benchmarks.json)
into Grafana.

## Roadmap

- Multi-sandbox concurrent readers and writers
- Cold-mount time to first byte
- Large-file random reads
- Trace-derived reconstruction extent ordering from a live Xet download

FUSE and object-store telemetry remains out of scope for the pyperf schema.
Correlate it externally by benchmark timestamp. Useful external measurements
include FUSE request sizes and latency, in-flight requests, object operation
concurrency, staged bytes, and cache hits.
