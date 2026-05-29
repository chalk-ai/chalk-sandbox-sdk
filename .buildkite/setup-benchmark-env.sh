#!/usr/bin/env bash
#
# Source this from benchmark steps to set up the environment.
# Installs sops, decrypts secrets, installs uv + deps + chalkcompute-benchmark.
#
set -euo pipefail
unset VIRTUAL_ENV

echo "--- :gear: Install sops"
SOPS_VERSION=3.9.4
curl -fsSL "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" -o /tmp/sops
chmod +x /tmp/sops

echo "--- :key: Decrypt secrets"
ENV_FILE="${BENCHMARK_ENV_FILE:-.env.enc}"
/tmp/sops --input-type dotenv --output-type dotenv -d "$ENV_FILE" > /tmp/test.env
set -a
source /tmp/test.env
set +a
rm /tmp/test.env

echo "--- :python: Install uv"
source .buildkite/install-uv.sh

echo "--- :python: Sync dependencies"
uv sync --locked --all-extras --reinstall-package chalkcompute

echo "--- :python: Install chalkcompute-benchmark"
CHALKCOMPUTE_BENCHMARK_DIR="$(mktemp -d)"
git clone --depth 1 "git@github.com:chalk-ai/chalkcompute-benchmark.git" "$CHALKCOMPUTE_BENCHMARK_DIR"
uv pip install --no-deps "$CHALKCOMPUTE_BENCHMARK_DIR"
