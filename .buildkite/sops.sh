#!/usr/bin/env bash
set -euo pipefail

PROJECT=chalk-develop
LOCATION=us-central1
REPOSITORY=third-party-binaries
PACKAGE=sops
DOWNLOAD_DIR=

cleanup() {
  if [[ -n "$DOWNLOAD_DIR" ]]; then
    rm -rf "$DOWNLOAD_DIR"
  fi
}
trap cleanup EXIT

latest_promoted_version() {
  gcloud artifacts versions list \
    --project="$PROJECT" \
    --location="$LOCATION" \
    --repository="$REPOSITORY" \
    --package="$PACKAGE" \
    --sort-by='~createTime' \
    --limit=1 \
    --format='value(name.basename())'
}

artifact_exists() {
  gcloud artifacts versions describe "$1" \
    --project="$PROJECT" \
    --location="$LOCATION" \
    --repository="$REPOSITORY" \
    --package="$PACKAGE" >/dev/null 2>&1
}

download_from_gar() {
  local version="$1"
  local destination="$2"
  local file="sops-v${version}.linux.amd64"

  gcloud artifacts generic download \
    --quiet \
    --project="$PROJECT" \
    --location="$LOCATION" \
    --repository="$REPOSITORY" \
    --package="$PACKAGE" \
    --version="$version" \
    --name="$file" \
    --destination="$destination"
}

install_sops() {
  local version
  local file

  version="$(latest_promoted_version)"
  if [[ -z "$version" ]]; then
    echo "No promoted SOPS version found" >&2
    exit 1
  fi

  file="sops-v${version}.linux.amd64"
  DOWNLOAD_DIR="$(mktemp -d)"
  download_from_gar "$version" "$DOWNLOAD_DIR"
  install -m 0755 "$DOWNLOAD_DIR/$file" /tmp/sops
  /tmp/sops --version
}

promote_sops() {
  local version="${1:-}"
  local file
  local release_url
  local expected_sha256

  if [[ -z "$version" ]]; then
    version="$(curl -fsSL https://api.github.com/repos/getsops/sops/releases/latest | jq -r '.tag_name')"
  fi
  version="${version#v}"

  if artifact_exists "$version"; then
    echo "SOPS $version is already promoted"
    exit 0
  fi

  file="sops-v${version}.linux.amd64"
  release_url="https://github.com/getsops/sops/releases/download/v${version}"
  DOWNLOAD_DIR="$(mktemp -d)"

  curl -fL --retry 4 --retry-all-errors --connect-timeout 10 \
    "$release_url/$file" \
    -o "$DOWNLOAD_DIR/$file"
  curl -fL --retry 4 --retry-all-errors --connect-timeout 10 \
    "$release_url/sops-v${version}.checksums.txt" \
    -o "$DOWNLOAD_DIR/checksums.txt"

  expected_sha256="$(awk -v file="$file" '$2 == file { print $1 }' "$DOWNLOAD_DIR/checksums.txt")"
  if [[ -z "$expected_sha256" ]]; then
    echo "No checksum found for $file" >&2
    exit 1
  fi
  echo "$expected_sha256  $DOWNLOAD_DIR/$file" | sha256sum --check

  chmod +x "$DOWNLOAD_DIR/$file"
  "$DOWNLOAD_DIR/$file" --version
  "$DOWNLOAD_DIR/$file" \
    --input-type dotenv \
    --output-type dotenv \
    --decrypt .env.enc >/dev/null

  gcloud artifacts generic upload \
    --quiet \
    --project="$PROJECT" \
    --location="$LOCATION" \
    --repository="$REPOSITORY" \
    --package="$PACKAGE" \
    --version="$version" \
    --source="$DOWNLOAD_DIR/$file"

  echo "Promoted SOPS $version"
}

case "${1:-}" in
  install)
    install_sops
    ;;
  promote)
    promote_sops "${2:-}"
    ;;
  *)
    echo "usage: $0 install | promote [version]" >&2
    exit 2
    ;;
esac
