#!/usr/bin/env python3
"""
Execute a command in a sandbox and stream output.

Usage:
    ./sandbox_exec.py --target localhost:50051 --sandbox-id <id> --exec "echo hello"
"""

import argparse
import shlex
import sys

from sandbox import SandboxClient


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Execute a command in a sandbox and stream output"
    )
    parser.add_argument(
        "--target", required=True, help="gRPC target (e.g., localhost:50051)"
    )
    parser.add_argument("--sandbox-id", required=True, help="Sandbox ID")
    parser.add_argument(
        "--exec", required=True, dest="command", help="Command to execute"
    )
    parser.add_argument("--workdir", help="Working directory")
    parser.add_argument("--timeout", type=int, help="Timeout in seconds")
    args = parser.parse_args()

    client = SandboxClient(args.target)
    sandbox = client.get(id=args.sandbox_id)

    parts = shlex.split(args.command)
    command = parts[0]
    command_args = parts[1:] if len(parts) > 1 else []

    for event in sandbox.exec_stream(
        command,
        *command_args,
        timeout_secs=args.timeout,
        workdir=args.workdir,
    ):
        if event.stdout:
            print(event.stdout, end="", flush=True)
        if event.stderr:
            print(event.stderr, end="", file=sys.stderr, flush=True)
        if event.is_error:
            print(f"Error: {event.error_code}: {event.error_message}", file=sys.stderr)
            sys.exit(1)
        if event.is_exited:
            sys.exit(event.exit_code or 0)


if __name__ == "__main__":
    main()
