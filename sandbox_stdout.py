#!/usr/bin/env python3
"""
Execute a command in a sandbox interactively, with stdin support.

Usage:
    ./sandbox_stdout.py --target localhost:50051 --sandbox-id <id> --exec "bash"
"""

import argparse
import shlex
import sys
import threading

from sandbox import SandboxClient


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Execute a command interactively in a sandbox"
    )
    parser.add_argument(
        "--target", required=True, help="gRPC target (e.g., localhost:50051)"
    )
    parser.add_argument("--sandbox-id", required=True, help="Sandbox ID")
    parser.add_argument(
        "--exec", required=True, dest="command", help="Command to execute"
    )
    args = parser.parse_args()

    client = SandboxClient(args.target)
    sandbox = client.get(id=args.sandbox_id)

    parts = shlex.split(args.command)
    command = parts[0]
    command_args = parts[1:] if len(parts) > 1 else []

    process = sandbox.exec_start(command, *command_args)

    # Read stdin in a background thread
    def read_stdin() -> None:
        try:
            for line in sys.stdin:
                process.write_stdin(line)
            process.close_stdin()
        except Exception:
            process.close_stdin()

    stdin_thread = threading.Thread(target=read_stdin, daemon=True)
    stdin_thread.start()

    for event in process.output():
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
