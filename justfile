# Chalk Sandbox SDK

buildkite_org := "chalk"
buildkite_pipeline := "chalk-sandbox-sdk-integration-tests"

# Run ty type checker
ty:
    uv run --with ty ty check container.py sandbox.py image.py volume.py

# Run ruff linter
ruff:
    uv run --with ruff ruff check container.py sandbox.py image.py volume.py

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
