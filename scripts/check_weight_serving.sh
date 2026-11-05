#!/usr/bin/env bash
# Reusable CPU-only validation. Uses the current SDK environment.
set -euo pipefail
cd "$(dirname "$0")/.."
python_bin="${PYTHON:-python}"
"$python_bin" -m pytest tests/test_weight_serving.py -q "$@"
"$python_bin" -m ruff check chalkcompute/weights/serving tests/test_weight_serving.py
"$python_bin" -m ruff format --check chalkcompute/weights/serving tests/test_weight_serving.py
"$python_bin" -m compileall -q chalkcompute/weights/serving
