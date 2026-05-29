#!/usr/bin/env bash
#
# Shared harness for running a benchmark and uploading pyperf results.
#
# Usage:
#   .buildkite/run-benchmark.sh \
#     --suite "Sandbox TTI Benchmarks" \
#     --case  "sandbox-tti" \
#     -- python -m sandbox_benchmark.pyperf_adapter --mode all --iterations 20
#
# The command after -- must write pyperf JSON to the path in
# $CHALK_BENCHMARK_PYPERF_OUTPUT (set automatically by this script).
#
set -euo pipefail

SUITE_NAME=""
CASE_NAME=""
TAGS=""
BENCHMARK_CMD=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --suite) SUITE_NAME="$2"; shift 2 ;;
    --case)  CASE_NAME="$2";  shift 2 ;;
    --tags)  TAGS="$2";       shift 2 ;;
    --)      shift; BENCHMARK_CMD=("$@"); break ;;
    *)       echo "Unknown flag: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$SUITE_NAME" || -z "$CASE_NAME" || ${#BENCHMARK_CMD[@]} -eq 0 ]]; then
  echo "Usage: $0 --suite <name> --case <name> [--tags <t1,t2>] -- <command...>" >&2
  exit 1
fi

: "${BENCHMARK_RESULTS_URL:?must be set}"
: "${BENCHMARK_REPORTER_IMAGE:?must be set}"

PYPERF_OUTPUT="$(mktemp /tmp/benchmark-XXXXXX.json)"
export CHALK_BENCHMARK_PYPERF_OUTPUT="$PYPERF_OUTPUT"

echo "--- :bar_chart: Run benchmark: ${CASE_NAME}"
"${BENCHMARK_CMD[@]}"

if [[ ! -s "$PYPERF_OUTPUT" ]]; then
  echo "Benchmark produced no output at $PYPERF_OUTPUT" >&2
  exit 1
fi

UPLOAD_ARGS=(
  --mode upload-json
  --format pyperf
  --input-json-file /results/results.json
  --suite-name "$SUITE_NAME"
  --case-name "$CASE_NAME"
  --results-url "$BENCHMARK_RESULTS_URL"
  --source ci
)
[[ -n "${BUILDKITE_BUILD_URL:-}" ]] && UPLOAD_ARGS+=(--triggered-by "$BUILDKITE_BUILD_URL")
[[ -n "$TAGS" ]] && UPLOAD_ARGS+=(--tags "$TAGS")

echo "--- :rocket: Upload results: ${CASE_NAME}"
docker run --rm \
  -v "${PYPERF_OUTPUT}:/results/results.json:ro" \
  "${BENCHMARK_REPORTER_IMAGE}" \
  "${UPLOAD_ARGS[@]}"

rm -f "$PYPERF_OUTPUT"
