#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx", "websockets"]
# ///
"""
Execute a command in a sandbox and stream stdout.

Usage:
    ./sandbox_exec.py --control-plane http://localhost:8080 --sandbox-id <id> --exec "echo hello"
"""

import argparse
import asyncio
import shlex

from sandbox import SandboxClient


async def main() -> None:
    parser = argparse.ArgumentParser(description="Execute a command in a sandbox and stream stdout")
    parser.add_argument("--control-plane", required=True, help="Control plane URI (e.g., http://localhost:8080)")
    parser.add_argument("--sandbox-id", required=True, help="Sandbox ID")
    parser.add_argument("--exec", required=True, dest="command", help="Command to execute")
    args = parser.parse_args()

    client = SandboxClient(args.control_plane)
    sandbox = client.get(id=args.sandbox_id)

    # Parse the command string into command + args
    parts = shlex.split(args.command)
    command = parts[0]
    command_args = parts[1:] if len(parts) > 1 else []

    # Start the exec (this triggers the process)
    # We need to run exec and listen concurrently
    async def run_exec() -> None:
        # Run exec in a thread since it's synchronous
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: sandbox.exec(command, *command_args))
        if result.error:
            print(f"Error: {result.error}")

    async def listen_stdout() -> None:
        async for data in sandbox.stdout.listen():
            print(data, end="", flush=True)

    # Run both concurrently - exec triggers the command, listen gets the output
    await asyncio.gather(run_exec(), listen_stdout())


if __name__ == "__main__":
    asyncio.run(main())
