#!/usr/bin/env sh

# Prepare and read synthetic model shards inside a sandbox.

set -eu

FIO_SHARD_SIZE="${FIO_SHARD_SIZE:-256M}"
FIO_SHARDS="${FIO_SHARDS:-4}"
FIO_BLOCK_SIZE="${FIO_BLOCK_SIZE:-16M}"

positive_integer() {
  name=$1
  value=$2
  case "$value" in
    *[!0-9]* | '') echo "$name must be a positive integer" >&2; exit 2 ;;
  esac
  if [ "$value" -eq 0 ]; then
    echo "$name must be a positive integer" >&2
    exit 2
  fi
}

positive_integer FIO_SHARDS "$FIO_SHARDS"

prepare() {
  model_dir=$1
  mkdir -p "$model_dir"
  fio --name=prepare-model-shards --directory="$model_dir" \
    --ioengine=psync --rw=write --bs="$FIO_BLOCK_SIZE" --direct=0 --thread \
    --filesize="$FIO_SHARD_SIZE" --nrfiles=1 --numjobs="$FIO_SHARDS" \
    --filename_format='model-shard.$jobnum' --end_fsync=1 --refill_buffers \
    --fallocate=none --group_reporting --output-format=json+ \
    --output="$model_dir/prepare.json"
  printf '%s\n' "$FIO_SHARDS" >"$model_dir/shard-count"
}

run_read() {
  name=$1
  model_dir=$2
  results_dir=$3
  jobs=$4
  invalidate=$5
  fio --name="$name" --directory="$model_dir" \
    --ioengine=psync --rw=read --bs="$FIO_BLOCK_SIZE" --direct=0 --thread \
    --filesize="$FIO_SHARD_SIZE" --nrfiles=1 --numjobs="$jobs" \
    --filename_format='model-shard.$jobnum' --allow_file_create=0 \
    --fadvise_hint=sequential --invalidate="$invalidate" --group_reporting \
    --output-format=json+ --output="$results_dir/$name.json"
}

read_shards() {
  model_dir=$1
  results_dir=$2
  jobs=$3
  positive_integer jobs "$jobs"
  if [ "$jobs" -gt "$FIO_SHARDS" ]; then
    echo "jobs must not exceed FIO_SHARDS" >&2
    exit 2
  fi

  mkdir -p "$results_dir"
  run_read "fresh-read-jobs-$jobs" "$model_dir" "$results_dir" "$jobs" 1
  run_read "warm-read-jobs-$jobs" "$model_dir" "$results_dir" "$jobs" 0
  touch "$results_dir/jobs-$jobs.complete"
}

case "${1:-}" in
  prepare)
    [ "$#" -eq 2 ] || { echo "usage: $0 prepare MODEL_DIR" >&2; exit 2; }
    prepare "$2"
    ;;
  read)
    [ "$#" -eq 4 ] || { echo "usage: $0 read MODEL_DIR RESULTS_DIR JOBS" >&2; exit 2; }
    read_shards "$2" "$3" "$4"
    ;;
  *)
    echo "usage: $0 {prepare MODEL_DIR|read MODEL_DIR RESULTS_DIR JOBS}" >&2
    exit 2
    ;;
esac
