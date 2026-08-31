#!/usr/bin/env sh

# Run filesystem I/O tests in a mounted sandbox volume.
# Usage: FIO_SIZE=1G FIO_JOBS=4 ./payloads/fio.sh /volume/fio-benchmark
# Defaults: /volume/fio-benchmark-<timestamp>, 1G per large-file job, one job.

set -eu

FIO_RUN_ID="${FIO_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
FIO_DIR="${FIO_DIR:-/volume/fio-benchmark-${FIO_RUN_ID}}"
FIO_SIZE="${FIO_SIZE:-1G}"
FIO_JOBS="${FIO_JOBS:-1}"
FIO_SMALL_FILES="${FIO_SMALL_FILES:-1000}"
FIO_SMALL_FILE_SIZE="${FIO_SMALL_FILE_SIZE:-4k}"
FIO_DIRECT="${FIO_DIRECT:-1}"

if [ "$#" -gt 1 ]; then
  echo "usage: $0 [benchmark-directory]" >&2
  exit 2
fi
if [ "$#" -eq 1 ]; then
  FIO_DIR=$1
fi

case "$FIO_JOBS" in
  *[!0-9]* | '') echo "FIO_JOBS must be a positive integer" >&2; exit 2 ;;
esac
if [ "$FIO_JOBS" -eq 0 ]; then
  echo "FIO_JOBS must be a positive integer" >&2
  exit 2
fi
case "$FIO_DIRECT" in
  0 | 1) ;;
  *) echo "FIO_DIRECT must be 0 or 1" >&2; exit 2 ;;
esac

install_fio() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends fio
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache fio
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y fio
  elif command -v yum >/dev/null 2>&1; then
    yum install -y fio
  else
    echo "fio is required and no supported package manager was found" >&2
    exit 1
  fi
}

if ! command -v fio >/dev/null 2>&1; then
  install_fio
fi

mkdir -p "$FIO_DIR"
RESULTS_DIR="$FIO_DIR/results"
mkdir -p "$RESULTS_DIR"

echo "fio version: $(fio --version)"
echo "benchmark directory: $FIO_DIR"
echo "large-file size per job: $FIO_SIZE"
echo "large-file jobs: $FIO_JOBS"
echo "direct I/O: $FIO_DIRECT"

run_fio() {
  name=$1
  shift
  echo
  echo "=== $name ==="
  fio --name="$name" --directory="$FIO_DIR" --group_reporting \
    --ioengine=psync --direct="$FIO_DIRECT" --thread \
    --output-format=json+ --output="$RESULTS_DIR/$name.json" "$@"
}

# Each large-file job owns one file so concurrent jobs do not contend on one inode.
run_fio sequential-write \
  --rw=write --bs=4M --filesize="$FIO_SIZE" --nrfiles=1 \
  --numjobs="$FIO_JOBS" --openfiles=1 --end_fsync=1 --refill_buffers \
  --filename_format='large.$jobnum'

run_fio sequential-read \
  --rw=read --bs=4M --filesize="$FIO_SIZE" --nrfiles=1 \
  --numjobs="$FIO_JOBS" --openfiles=1 --filename_format='large.$jobnum'

run_fio random-read-4k \
  --rw=randread --bs=4k --filesize="$FIO_SIZE" --nrfiles=1 \
  --numjobs="$FIO_JOBS" --openfiles=1 --filename_format='large.$jobnum'

run_fio random-write-4k \
  --rw=randwrite --bs=4k --filesize="$FIO_SIZE" --nrfiles=1 \
  --numjobs="$FIO_JOBS" --openfiles=1 --end_fsync=1 --refill_buffers \
  --filename_format='random-write.$jobnum'

# One job closes each small file before moving to the next, exercising metadata and sync behavior.
run_fio small-file-write \
  --rw=write --bs="$FIO_SMALL_FILE_SIZE" --filesize="$FIO_SMALL_FILE_SIZE" \
  --nrfiles="$FIO_SMALL_FILES" --numjobs=1 --openfiles=1 \
  --file_service_type=sequential --end_fsync=1 --refill_buffers \
  --filename_format='small.$filenum'

run_fio small-file-read \
  --rw=read --bs="$FIO_SMALL_FILE_SIZE" --filesize="$FIO_SMALL_FILE_SIZE" \
  --nrfiles="$FIO_SMALL_FILES" --numjobs=1 --openfiles=1 \
  --file_service_type=sequential --filename_format='small.$filenum'

echo
echo "Raw fio JSON results: $RESULTS_DIR"
echo "Benchmark data remains at: $FIO_DIR"
