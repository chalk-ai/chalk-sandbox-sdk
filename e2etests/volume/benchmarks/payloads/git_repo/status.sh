#!/bin/sh

set -u

mount_path=$1
result_path=$2
log_path=$3
status_output=/tmp/git-status-output

started_ns=$(date +%s%N)
git -C "$mount_path/vllm" status --porcelain=v1 --untracked-files=all \
  >"$status_output" 2>"$log_path"
status=$?
finished_ns=$(date +%s%N)
dirty_paths=$(wc -l <"$status_output")
printf '{"elapsed_ns":%s,"dirty_paths":%s}\n' \
  "$((finished_ns - started_ns))" "$dirty_paths" >"$result_path"
if [ "$dirty_paths" -ne 0 ]; then
  cat "$status_output" >>"$log_path"
fi
exit "$status"
