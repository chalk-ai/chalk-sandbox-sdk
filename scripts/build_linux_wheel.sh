#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Build a Linux Python wheel, even from macOS, using Docker.

Usage:
  scripts/build_linux_wheel.sh [--repo PATH] [--python 3.11] [--platform linux/amd64] [--out-dir dist/linux-local]

Options:
  --repo PATH            Path to the Python package repo to build.
                         Default: .
  --python VERSION       Python version to build against.
                         Default: 3.11
  --platform VALUE       Docker target platform.
                         Default: linux/amd64
  --out-dir PATH         Output directory inside the built repo.
                         Default: dist/linux-local
  --help                 Show this message.

Notes:
  - This script requires Docker.
  - On macOS, Docker Desktop must support the requested Linux platform.
  - The resulting wheel is intended for Linux remote function pods, not local macOS use.
EOF
}

repo_path="."
python_version="3.11"
docker_platform="linux/amd64"
out_dir="dist/linux-local"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)
            repo_path="$2"
            shift 2
            ;;
        --python)
            python_version="$2"
            shift 2
            ;;
        --platform)
            docker_platform="$2"
            shift 2
            ;;
        --out-dir)
            out_dir="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required" >&2
    exit 1
fi

repo_path="$(cd "$repo_path" && pwd)"
if [[ ! -f "$repo_path/pyproject.toml" ]]; then
    echo "No pyproject.toml found in repo: $repo_path" >&2
    exit 1
fi

repo_name="$(basename "$repo_path")"
tmp_out="$(mktemp -d)"
trap 'rm -rf "$tmp_out"' EXIT

echo "Building Linux wheel for $repo_name"
echo "  repo:      $repo_path"
echo "  python:    $python_version"
echo "  platform:  $docker_platform"
echo "  out dir:   $out_dir"

docker run --rm \
    --platform "$docker_platform" \
    -v "$repo_path:/src" \
    -v "$tmp_out:/out" \
    "python:${python_version}-slim" \
    bash -lc "
        set -euo pipefail
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y --no-install-recommends build-essential ca-certificates curl git
        curl https://sh.rustup.rs -sSf | sh -s -- -y --profile minimal
        . /root/.cargo/env
        python -m pip install --upgrade pip build setuptools wheel setuptools-rust
        cd /src
        python -m build --wheel --outdir /out
    "

mkdir -p "$repo_path/$out_dir"

mapfile -t built_wheels < <(find "$tmp_out" -maxdepth 1 -type f -name '*.whl' | sort)
if [[ ${#built_wheels[@]} -eq 0 ]]; then
    echo "No wheel was produced" >&2
    exit 1
fi

copied=()
for wheel in "${built_wheels[@]}"; do
    dest="$repo_path/$out_dir/$(basename "$wheel")"
    cp "$wheel" "$dest"
    copied+=("$dest")
done

echo "Built wheel(s):"
for wheel in "${copied[@]}"; do
    echo "  $wheel"
done
