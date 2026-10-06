#!/bin/sh

mount_path=$1
log_path=$2
commit_log=/tmp/chalk-volume-commit.log

"$mount_path/.chalk-volume/commit" >"$commit_log" 2>&1
status=$?
cat "$commit_log" >>"$log_path" || true
if grep -Fq 'no space left on device (staged data cap reached)' "$commit_log"; then
  exit 75
fi
exit "$status"
