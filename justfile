# Chalk Sandbox SDK

buildkite_org := "chalk"
buildkite_pipeline := "chalk-sandbox-sdk-integration-tests"

# Run all e2e tests in CI and print a link to the build
ci-run:
    #!/usr/bin/env bash
    set -euo pipefail
    branch="$(git rev-parse --abbrev-ref HEAD)"
    commit="$(git rev-parse HEAD)"
    echo "Triggering Buildkite build on branch ${branch} (${commit})..."
    build_url=$(curl -sS -X POST \
        "https://api.buildkite.com/v2/organizations/{{ buildkite_org }}/pipelines/{{ buildkite_pipeline }}/builds" \
        -H "Authorization: Bearer ${BUILDKITE_API_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"commit\": \"${commit}\", \"branch\": \"${branch}\", \"message\": \"Triggered via just ci-run\"}" \
        | jq -r '.web_url')
    echo ""
    echo "Build started: ${build_url}"
