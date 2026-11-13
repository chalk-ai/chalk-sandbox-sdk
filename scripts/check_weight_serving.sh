#!/usr/bin/env bash
# Reusable CPU-only validation. Uses the current SDK environment; no compute claim.
set -euo pipefail
cd "$(dirname "$0")/.."
python_bin="${PYTHON:-python}"
test_files=(
  tests/test_weights_codec.py tests/test_weights_stream.py
  tests/test_weight_serving.py tests/test_sglang_reload.py
  tests/test_weight_layout.py tests/test_resident.py tests/test_resident_runtime.py
  tests/test_vllm_resident.py tests/test_sglang_resident.py
  tests/test_resident_control.py tests/test_resident_engine_bridges.py
  tests/test_sparse_weight_http.py
  tests/test_managed_weight_target.py tests/test_model_target_delivery.py
)
"$python_bin" -m pytest "${test_files[@]}" -q "$@"
"$python_bin" -m ruff check chalkcompute/weights "${test_files[@]}" scripts/check_sparse_weight_http.py
"$python_bin" -m ruff format --check chalkcompute/weights "${test_files[@]}" scripts/check_sparse_weight_http.py
"$python_bin" -m compileall -q chalkcompute/weights scripts/check_sparse_weight_http.py
