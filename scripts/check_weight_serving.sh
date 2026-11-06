#!/usr/bin/env bash
# Reusable CPU-only validation. Uses the current SDK environment.
set -euo pipefail
cd "$(dirname "$0")/.."
python_bin="${PYTHON:-python}"
test_files=(tests/test_weight_serving.py tests/test_sglang_reload.py tests/test_vllm_resident_sparse.py)
"$python_bin" -m pytest "${test_files[@]}" -q "$@"
"$python_bin" -m ruff check chalkcompute/weights/serving "${test_files[@]}"
"$python_bin" -m ruff format --check chalkcompute/weights/serving "${test_files[@]}"
"$python_bin" -m compileall -q chalkcompute/weights/serving
