# Chalk Sandbox SDK

# Get list of just commands
default:
    @just --list

buildkite_org := "chalk"
buildkite_pipeline := "chalk-sandbox-sdk-integration-tests"

# Decrypt .env.enc to .env (or .env.<name>.enc to .env.<name> if name given)
env name="":
    sops --input-type dotenv --output-type dotenv -d ".env{{ if name != "" { "." + name } else { "" } }}.enc" > ".env{{ if name != "" { "." + name } else { "" } }}"

# Encrypt .env (or .env.<name>) to .env.enc (or .env.<name>.enc) and delete plaintext
encrypt name="":
    sops --input-type dotenv --output-type dotenv -e ".env{{ if name != "" { "." + name } else { "" } }}" > ".env{{ if name != "" { "." + name } else { "" } }}.enc"
    rm ".env{{ if name != "" { "." + name } else { "" } }}"

# Sync all dependencies
sync:
    uv sync --all-extras

# Build a Linux chalk-remote-call-python wheel via Docker.
# Example:
#   just remote-call-wheel-build ../chalk-remote-call-python 3.11
remote-call-wheel-build repo="../chalk-remote-call-python" python="3.11" platform="linux/amd64" out_dir="dist/linux-local":
    ./scripts/build_remote_call_linux_wheel.sh --repo {{repo}} --python {{python}} --platform {{platform}} --out-dir {{out_dir}}

# Print the recommended env vars for deploying a function against local SDK code.
# Example:
#   just local-fn-env /abs/path/to/chalk_remote_call_python-...whl
local-fn-env remote_call_wheel="":
    @echo "CHALK_FUNCTION_STRIP_SOURCE=0"
    @echo "CHALK_FUNCTION_LOCAL_SDK_COPY=1"
    @if [ -n "{{remote_call_wheel}}" ]; then echo "CHALK_FUNCTION_REMOTE_CALL_WHEEL={{remote_call_wheel}}"; fi

# Run a deploy script with the recommended local-dev env vars.
# Example:
#   just local-fn-deploy examples/scalar/deploy_sync.py ../chalk-remote-call-python/dist/linux-local/chalk_remote_call_python-0.0.0-cp311-cp311-linux_x86_64.whl
local-fn-deploy script remote_call_wheel="" use_uv="1":
    ./scripts/run_local_fn_deploy.sh --script {{script}} {{ if remote_call_wheel != "" { "--remote-call-wheel " + remote_call_wheel } else { "" } }} {{ if use_uv == "1" { "--uv-run" } else { "" } }}

# Run ty type checker
ty:
    uv run --with ty ty check chalkcompute/ --exclude chalkcompute/_gen/

# Run unit tests
test *args:
    uv run python -m pytest tests/ {{args}}

# Run ruff linter
ruff:
    uv run --with ruff ruff check chalkcompute/ --exclude chalkcompute/_gen/

# Run ruff formatter
format:
    uv run --with ruff ruff format .

# Build a zoekt full-text index of the current codebase into .zoekt-index/
zoekt-index:
    #!/usr/bin/env bash
    set -euo pipefail
    repo_root="$(git rev-parse --show-toplevel)"
    index_dir="${repo_root}/.zoekt-index"
    rm -rf "${index_dir}"
    mkdir -p "${index_dir}"
    # Stage only git-tracked files under a directory named "chalk-sandbox-sdk"
    # so zoekt uses that as the repo name (it names repos after the indexed
    # directory's basename). Using git ls-files keeps .venv, dist, __pycache__,
    # etc. out of the index.
    staging="$(mktemp -d)"
    trap 'rm -rf "${staging}"' EXIT
    target="${staging}/chalk-sandbox-sdk"
    mkdir -p "${target}"
    (cd "${repo_root}" && git ls-files -z) | (cd "${repo_root}" && rsync -a --files-from=- -0 ./ "${target}/")
    zoekt-index -index "${index_dir}" -disable_ctags "${target}"
    echo "Index written to ${index_dir}"

# Examples: just zoekt-search 'SandboxClient'
#           just zoekt-search -l 'def run file:py'   (filenames only)
#           just zoekt-search 'has_many file:py'     (scope by filename regex)
#           just zoekt-search -jsonl 'DataFrame'     (JSON output)
# Search the zoekt index of this codebase (zoekt flags must precede the query).
zoekt-search +args:
    #!/usr/bin/env bash
    set -euo pipefail
    repo_root="$(git rev-parse --show-toplevel)"
    index_dir="${repo_root}/.zoekt-index"
    if [[ ! -d "${index_dir}" ]] || [[ -z "$(ls "${index_dir}"/*.zoekt 2>/dev/null)" ]]; then
        echo "No index found. Run: just zoekt-index" >&2
        exit 1
    fi
    exec zoekt -index_dir "${index_dir}" {{ args }}

# Create a new GitHub release (interactive version bump)
release:
    ./release.sh

# Run all e2e tests in CI and print a link to the build
ci-run:
    #!/usr/bin/env bash
    set -euo pipefail
    branch="$(git rev-parse --abbrev-ref HEAD)"
    commit="$(git rev-parse HEAD)"

    # Push latest commits so Buildkite picks them up
    echo "Pushing ${branch} to origin..."
    git push origin "${branch}" 2>&1

    # Find the PR number for this branch
    pr_number=$(gh pr view --json number --jq '.number' 2>/dev/null || true)
    if [[ -z "${pr_number}" ]]; then
        echo "No open PR found for branch ${branch}. Create one first with: gh pr create"
        exit 1
    fi

    # Poll for the Buildkite check URL (may take a few seconds to appear)
    echo "Waiting for Buildkite check on PR #${pr_number}..."
    for i in $(seq 1 15); do
        build_url=$(gh pr checks "${pr_number}" --json name,link \
            --jq '.[] | select(.name | test("buildkite"; "i")) | .link' 2>/dev/null | tail -1 || true)
        if [[ -n "${build_url}" ]]; then
            break
        fi
        sleep 2
    done

    if [[ -z "${build_url}" ]]; then
        echo "Could not find Buildkite check URL. Check the PR directly:"
        echo "  https://github.com/{{ buildkite_org }}/{{ buildkite_pipeline }}/pull/${pr_number}"
        exit 1
    fi

    echo ""
    echo "Build: ${build_url}"
