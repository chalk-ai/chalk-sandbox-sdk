#!/usr/bin/env bash
set -euo pipefail

UV_VERSION="${UV_VERSION:-0.8.15}"
export PATH="$HOME/.local/bin:$PATH"

if command -v uv >/dev/null 2>&1; then
  current_version="$(uv --version | awk '{print $2}')"
  if [ "$current_version" = "$UV_VERSION" ]; then
    uv --version
    return 0 2>/dev/null || exit 0
  fi
fi

curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
uv --version
