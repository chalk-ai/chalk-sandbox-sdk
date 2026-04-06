#!/usr/bin/env bash
# Regenerate Python gRPC stubs from the proto file.
# Requires: pip install grpcio-tools
set -euo pipefail

cd "$(dirname "$0")"

PROTO_OUT=chalkcompute/_proto

python3 -m grpc_tools.protoc \
  --proto_path=proto \
  --python_out="$PROTO_OUT" \
  --pyi_out="$PROTO_OUT" \
  --grpc_python_out="$PROTO_OUT" \
  proto/chalk/sandbox/v1/service.proto

# Fix generated imports: chalk.* -> chalkcompute._proto.*
find "$PROTO_OUT" \( -name '*.py' -o -name '*.pyi' \) -exec \
  sed -i '' 's/^from chalk\./from chalkcompute._proto./' {} +

echo "Generated stubs in $PROTO_OUT/chalk/sandbox/v1/"
