#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets"]
# ///
"""
Subscribe to a sandbox's stdout via WebSocket.

Usage:
    ./sandbox_stdout.py --control-plane http://localhost:8080 --sandbox-id <id>
"""

import argparse
import asyncio

import websockets


async def main() -> None:
    parser = argparse.ArgumentParser(description="Subscribe to a sandbox's stdout")
    parser.add_argument("--control-plane", required=True, help="Control plane URI (e.g., http://localhost:8080)")
    parser.add_argument("--sandbox-id", required=True, help="Sandbox ID")
    args = parser.parse_args()

    # Convert HTTP URL to WebSocket URL
    base = args.control_plane.rstrip("/")
    ws_url = base.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{ws_url}/v1/sandboxes/{args.sandbox_id}/stdout"

    async with websockets.connect(url) as ws:
        async for message in ws:
            if isinstance(message, bytes):
                print(message.decode("utf-8"), end="", flush=True)
            else:
                print(message, end="", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
