#!/usr/bin/env bash
# Regenerate Python gRPC stubs from the proto file.
# Requires: pip install grpcio-tools
set -euo pipefail

cd "$(dirname "$0")"

python3 -m grpc_tools.protoc \
  --proto_path=proto \
  --python_out=. \
  --pyi_out=. \
  --grpc_python_out=. \
  proto/chalk/sandbox/v1/service.proto

echo "Generated stubs in chalk/sandbox/v1/"
