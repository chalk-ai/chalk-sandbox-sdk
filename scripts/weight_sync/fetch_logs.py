"""Fetch bounded retained test logs using the repository's authoritative protobuf."""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import grpc
from google.protobuf import (
    descriptor_pb2,
    descriptor_pool,
    json_format,
    message_factory,
)

from ftqa_smoke import load_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--proto-root", type=Path, required=True)
    parser.add_argument("--cluster-id", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--access", action="store_true")
    parser.add_argument("--scaling-group-id")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-pages", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.max_pages <= 20:
        parser.error("max-pages must be in [1, 20]")
    load_env(args.env_file)
    from chalkcompute._connect import ConnectClient

    with tempfile.TemporaryDirectory(prefix="weight-log-schema-") as temporary:
        descriptor = Path(temporary) / "logs.pb"
        subprocess.run(
            [
                "protoc",
                "-I",
                str(args.proto_root),
                "--include_imports",
                f"--descriptor_set_out={descriptor}",
                "chalk/server/v1/log.proto",
            ],
            check=True,
        )
        files = descriptor_pb2.FileDescriptorSet.FromString(descriptor.read_bytes())
        pool = descriptor_pool.DescriptorPool()
        for file in files.file:
            pool.Add(file)
    method = "SearchAccessLogEntries" if args.access else "SearchLogEntries"
    request_type = message_factory.GetMessageClass(
        pool.FindMessageTypeByName(f"chalk.server.v1.{method}Request")
    )
    response_type = message_factory.GetMessageClass(
        pool.FindMessageTypeByName(f"chalk.server.v1.{method}Response")
    )
    client = ConnectClient()
    client._ensure_auth()
    metadata = [
        (key.lower(), value)
        for key, value in (client._headers or {}).items()
        if key.lower() in {"authorization", "x-chalk-env-id", "x-chalk-server"}
    ]
    endpoint = urlparse(client._api_server)
    body = {
        "query": args.query,
        "clusterId": args.cluster_id,
        "startTime": args.start,
        "endTime": args.end,
    }
    if args.scaling_group_id:
        if not args.access:
            parser.error(
                "scaling-group-id applies to --access only; use query for application logs"
            )
        body["scalingGroupId"] = args.scaling_group_id
    pages = []
    try:
        with grpc.secure_channel(
            endpoint.netloc, grpc.ssl_channel_credentials()
        ) as channel:
            call = channel.unary_unary(
                f"/chalk.server.v1.LogSearchService/{method}",
                request_serializer=lambda request: request.SerializeToString(),
                response_deserializer=response_type.FromString,
            )
            for _ in range(args.max_pages):
                request = json_format.ParseDict(body, request_type())
                page = json_format.MessageToDict(
                    call(request, metadata=metadata, timeout=60)
                )
                pages.append(page)
                args.out.write_text(json.dumps({"pages": pages}, indent=2))
                token = page.get("nextPageToken")
                if not token:
                    break
                body["pageToken"] = token
        key = "accessLogEntries" if args.access else "logEntries"
        print(
            json.dumps(
                {
                    "pages": len(pages),
                    "entries": sum(len(page.get(key, [])) for page in pages),
                    "out": str(args.out),
                }
            )
        )
    finally:
        if hasattr(client, "close"):
            client.close()


if __name__ == "__main__":
    main()
