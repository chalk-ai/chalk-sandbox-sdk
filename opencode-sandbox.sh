#!/usr/bin/env bash
set -euo pipefail

IMAGE="us-central1-docker.pkg.dev/chalk-develop/container-test/opencode"
SANDBOX_NAME="andy-opencode-$(date +%s)"

echo "Creating sandbox with image: $IMAGE"
SANDBOX_ID=$(chalk sandbox create \
  --image "$IMAGE" \
  --name "$SANDBOX_NAME" \
  --json \
  --no-spinners \
  | jq -r '.id')

if [[ -z "$SANDBOX_ID" || "$SANDBOX_ID" == "null" ]]; then
  echo "Failed to create sandbox" >&2
  exit 1
fi

echo "Sandbox created: $SANDBOX_ID"

cleanup() {
  echo ""
  echo "Terminating sandbox $SANDBOX_ID..."
  chalk sandbox terminate --id "$SANDBOX_ID" --no-spinners 2>/dev/null || true
}
trap cleanup EXIT

echo "Launching opencode..."
chalk sandbox exec "$SANDBOX_ID" -t -- opencode
