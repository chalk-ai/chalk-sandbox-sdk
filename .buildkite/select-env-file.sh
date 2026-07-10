#!/usr/bin/env bash

matrix="${1:?matrix value required}"

# Each matrix cell sources this script; a build-level target selects which cell should run.
if [[ -n "${CHALK_TARGET_ENV:-}" && "$CHALK_TARGET_ENV" != "$matrix" ]]; then
    echo "--- :fast_forward: Skipping $matrix; CHALK_TARGET_ENV=$CHALK_TARGET_ENV"
    exit 0
fi

case "$matrix" in
    "staging/ftqa")
        matrix_env_file=".env.enc"
        ;;
    "meta-ci")
        matrix_env_file=".env.meta-ci.enc"
        ;;
    *)
        echo "Unknown Buildkite matrix value: $matrix" >&2
        exit 1
        ;;
esac

export ENV_FILE="$matrix_env_file"
export BENCHMARK_ENV_FILE="$matrix_env_file"
