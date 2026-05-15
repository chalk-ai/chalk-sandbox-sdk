#!/usr/bin/env bash
set -euo pipefail

# Source this file from Buildkite steps so the PATH update persists.
UV_VERSION="${UV_VERSION:-0.11.14}"
UV_INSTALL_DIR="${UV_INSTALL_DIR:-/tmp/uv}"
export UV_INSTALL_DIR
export UV_NO_MODIFY_PATH=1
export PATH="${UV_INSTALL_DIR}:${PATH}"

current_version="$(uv --version 2>/dev/null | awk '{print $2}' || true)"
if [ "${current_version}" != "${UV_VERSION}" ]; then
  curl --proto '=https' --tlsv1.2 -LsSf \
    "https://releases.astral.sh/github/uv/releases/download/${UV_VERSION}/uv-installer.sh" \
    | sh
fi

actual_version="$(uv --version | awk '{print $2}')"
if [ "${actual_version}" != "${UV_VERSION}" ]; then
  echo "Expected uv ${UV_VERSION}, got ${actual_version}" >&2
  exit 1
fi

uv --version
