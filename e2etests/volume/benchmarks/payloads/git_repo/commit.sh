#!/bin/sh

set -u

mount_path=$1
result_path=$2
log_path=$3
repository="$mount_path/vllm"

: >"$log_path"
printf '\n<!-- Chalk volume benchmark commit -->\n' >>"$repository/README.md"

started_ns=$(date +%s%N)
git -C "$repository" commit -am "Benchmark mounted Git commit" \
  >>"$log_path" 2>&1
status=$?
finished_ns=$(date +%s%N)
commit_sha=
if [ "$status" -eq 0 ]; then
  commit_sha=$(git -C "$repository" rev-parse HEAD)
fi
printf '{"elapsed_ns":%s,"exit_code":%s,"commit_sha":"%s"}\n' \
  "$((finished_ns - started_ns))" "$status" "$commit_sha" >"$result_path"
exit "$status"
