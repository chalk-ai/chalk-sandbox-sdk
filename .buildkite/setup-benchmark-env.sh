#!/usr/bin/env bash
#
# Source this from benchmark steps to set up the environment.
# Installs sops, decrypts secrets, installs uv + deps + chalkcompute-benchmark.
#
set -euo pipefail
unset VIRTUAL_ENV

echo "--- :gear: Install sops"
.buildkite/sops.sh install

echo "--- :key: Decrypt secrets"
ENV_FILE="${BENCHMARK_ENV_FILE:-.env.enc}"
/tmp/sops --input-type dotenv --output-type dotenv -d "$ENV_FILE" > /tmp/test.env
set -a
source /tmp/test.env
set +a
rm /tmp/test.env

echo "--- :python: Bootstrap toolchain"
source .buildkite/install-uv.sh

echo "--- :package: Sync locked dependencies"
uv sync --locked --all-extras --reinstall-package chalkcompute

echo "--- :python: Install chalkcompute-benchmark"
CHALKCOMPUTE_BENCHMARK_DIR="$(mktemp -d)"
git clone --depth 1 "git@github.com:chalk-ai/chalk-compute-benchmark.git" "$CHALKCOMPUTE_BENCHMARK_DIR"
uv pip install --no-deps "$CHALKCOMPUTE_BENCHMARK_DIR"
