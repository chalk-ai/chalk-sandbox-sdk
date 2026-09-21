#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.13.0"]
# ///
"""Transfer bytes, streams, and local files through a sandbox filesystem.

This example requires standard Chalk credentials. It creates one ephemeral HOST
sandbox, which consumes billable compute while running, and terminates it on exit.
The local temporary files and every remote file are also discarded.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from chalkcompute import ComputeClass, SandboxClient


with SandboxClient.from_env() as client:
    sandbox = client.create(
        compute_class=ComputeClass.HOST,
        lifetime="600s",
    )
    try:
        # Small files can be transferred directly as bytes.
        sandbox.fs.write_bytes(
            "/tmp/greeting.txt",
            b"hello from the local process\n",
            mode=0o640,
        )
        print(sandbox.fs.read_bytes("/tmp/greeting.txt").decode(), end="")

        # Iterators keep larger content out of one monolithic bytes object. Input
        # chunks may have any size; the SDK applies the RPC's transfer chunking.
        sandbox.fs.upload_iter(
            (f"record {index}\n".encode() for index in range(10_000)),
            "/tmp/records.txt",
        )
        downloaded_size = sum(
            len(chunk) for chunk in sandbox.fs.download_iter("/tmp/records.txt")
        )
        print(f"Streamed {downloaded_size:,} bytes")

        # Path helpers stream between the sandbox and local disk. Downloads use
        # an adjacent temporary file and atomically replace the destination.
        with tempfile.TemporaryDirectory() as directory:
            local_source = Path(directory) / "input.txt"
            local_destination = Path(directory) / "output.txt"
            local_source.write_text("uploaded from a local file\n")

            sandbox.fs.upload_file(local_source, "/tmp/from-local.txt")
            sandbox.fs.download_file("/tmp/from-local.txt", local_destination)
            print(local_destination.read_text(), end="")
    finally:
        sandbox.terminate()
