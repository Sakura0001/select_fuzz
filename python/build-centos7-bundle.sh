#!/usr/bin/env bash
set -euo pipefail

# Build a self-contained x86_64 bundle that runs on CentOS 7 / glibc 2.17.
# The resulting directory contains its own CPython 3.11 runtime and all
# runtime wheels; the target machine does not need Python, pip, or uv.

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUT_DIR=${1:-"$ROOT_DIR/python/output"}
IMAGE_NAME=${IMAGE_NAME:-select-fuzz-centos7-builder}
PLATFORM=${PLATFORM:-linux/amd64}

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required to build the CentOS 7 bundle" >&2
  exit 2
fi

mkdir -p "$OUT_DIR"
echo "Building CentOS 7 bundle with ${PLATFORM}..."
docker build \
  --platform "$PLATFORM" \
  --file "$ROOT_DIR/packaging/centos7/Dockerfile" \
  --tag "$IMAGE_NAME" \
  "$ROOT_DIR"

container_id=$(docker create "$IMAGE_NAME")
cleanup() {
  docker rm "$container_id" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker cp \
  "$container_id:/out/select-fuzz-centos7-x86_64.tar.gz" \
  "$OUT_DIR/select-fuzz-centos7-x86_64.tar.gz"

rm -rf "$OUT_DIR/select-fuzz-centos7-x86_64"
mkdir -p "$OUT_DIR/select-fuzz-centos7-x86_64"
tar -xzf "$OUT_DIR/select-fuzz-centos7-x86_64.tar.gz" \
  --strip-components=1 \
  -C "$OUT_DIR/select-fuzz-centos7-x86_64"

echo "Bundle created: $OUT_DIR/select-fuzz-centos7-x86_64"
echo "Archive created: $OUT_DIR/select-fuzz-centos7-x86_64.tar.gz"
