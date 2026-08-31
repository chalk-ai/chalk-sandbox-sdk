#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <environment> <benchmark>" >&2
  exit 2
fi

environment=$1
benchmark=$2

source .buildkite/select-env-file.sh "$environment"
source .buildkite/setup-benchmark-env.sh

case "$benchmark" in
  fio)
    suite="Sandbox Volume FIO Benchmarks ($environment)"
    case_name="volume-fio"
    tags="sandbox,volume,fio,$environment"
    command=(
      uv run python -m e2etests.volume.benchmarks.fio
      --size 1G
      --jobs 1
      --small-files 1000
      --small-file-size 4k
      --direct 1
      --cpu 2
      --memory 4Gi
    )
    ;;
  model-load)
    suite="Sandbox Volume Model Load Benchmarks ($environment)"
    case_name="volume-model-load"
    tags="sandbox,volume,fio,model-load,$environment"
    command=(
      uv run python -m e2etests.volume.benchmarks.model_load
      --shards 4
      --shard-size 256M
      --block-size 16M
      --jobs 1,4
      --cpu 2
      --memory 4Gi
    )
    ;;
  model-download)
    suite="Sandbox Volume Model Download Benchmarks ($environment)"
    case_name="volume-model-download"
    tags="sandbox,volume,model-download,$environment"
    command=(
      uv run python -m e2etests.volume.benchmarks.model_download
      --large-shard-mib 1024
      --small-shard-mib 35
      --extent-mib 4
      --cpu 2
      --memory 2Gi
    )
    ;;
  *)
    echo "unknown volume benchmark: $benchmark" >&2
    exit 2
    ;;
esac

.buildkite/run-benchmark.sh \
  --suite "$suite" \
  --case "$case_name" \
  --tags "$tags" \
  -- "${command[@]}"
