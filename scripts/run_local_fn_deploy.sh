#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Run a function deploy script with the recommended local-dev env vars.

Usage:
  scripts/run_local_fn_deploy.sh --script examples/scalar/deploy_sync.py [--sdk-wheel /abs/path.whl] [--python .venv/bin/python] [--uv-run]

Options:
  --script PATH           Python deploy script to run.
  --sdk-wheel             Optional Linux chalkcompute wheel path.
  --python PATH           Python interpreter to run when not using uv.
                          Default: .venv/bin/python
  --uv-run                Run through "uv run python" instead of a direct interpreter.
  --no-local-sdk          Do not mount the local chalkcompute source overlay.
  --strip-source          Force strip mode on instead of the default local mode.
  --help                  Show this message.

Examples:
  scripts/run_local_fn_deploy.sh \
      --script examples/scalar/deploy_sync.py \
      --sdk-wheel dist/linux-local/chalkcompute-0.0.0-cp311-cp311-linux_x86_64.whl \
      --uv-run
EOF
}

script_path=""
sdk_wheel=""
python_bin=".venv/bin/python"
use_uv_run=0
use_local_sdk=1
strip_source=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --script)
            script_path="$2"
            shift 2
            ;;
        --sdk-wheel)
            sdk_wheel="$2"
            shift 2
            ;;
        --python)
            python_bin="$2"
            shift 2
            ;;
        --uv-run)
            use_uv_run=1
            shift
            ;;
        --no-local-sdk)
            use_local_sdk=0
            shift
            ;;
        --strip-source)
            strip_source=1
            shift
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

if [[ -z "$script_path" ]]; then
    echo "--script is required" >&2
    usage >&2
    exit 1
fi

if [[ ! -f "$script_path" ]]; then
    echo "Script not found: $script_path" >&2
    exit 1
fi

if [[ -n "$sdk_wheel" && ! -f "$sdk_wheel" ]]; then
    echo "SDK wheel not found: $sdk_wheel" >&2
    exit 1
fi

export CHALK_FUNCTION_STRIP_SOURCE="$strip_source"
if [[ "$use_local_sdk" == "1" && "$strip_source" == "0" ]]; then
    export CHALK_FUNCTION_LOCAL_SDK_COPY=1
else
    unset CHALK_FUNCTION_LOCAL_SDK_COPY || true
fi

if [[ -n "$sdk_wheel" ]]; then
    export CHALK_FUNCTION_LOCAL_SDK_WHEEL="$(cd "$(dirname "$sdk_wheel")" && pwd)/$(basename "$sdk_wheel")"
else
    unset CHALK_FUNCTION_LOCAL_SDK_WHEEL || true
fi

unset CHALK_FUNCTION_REMOTE_CALL_WHEEL || true

echo "Deploy env:"
echo "  CHALK_FUNCTION_STRIP_SOURCE=${CHALK_FUNCTION_STRIP_SOURCE}"
if [[ "${CHALK_FUNCTION_LOCAL_SDK_COPY:-}" == "1" ]]; then
    echo "  CHALK_FUNCTION_LOCAL_SDK_COPY=1"
fi
if [[ -n "${CHALK_FUNCTION_LOCAL_SDK_WHEEL:-}" ]]; then
    echo "  CHALK_FUNCTION_LOCAL_SDK_WHEEL=${CHALK_FUNCTION_LOCAL_SDK_WHEEL}"
fi

if [[ "$use_uv_run" == "1" ]]; then
    exec uv run python "$script_path"
fi

exec "$python_bin" "$script_path"
