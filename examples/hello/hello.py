#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Minimal example: spin up a Debian container, run a few commands, tear it down.

Usage:
    just env                          # decrypt .env (first time only)
    uv run python examples/hello/hello.py
"""

from __future__ import annotations

import os
import sys

# Load .env for credentials
_env_path = os.path.join(os.path.dirname(__file__), "../../.env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

# Ensure chalkcompute is importable from the repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from chalkcompute import Container, Image

# 1. Define an image: Python 3.12 slim + cowsay
image = Image.debian_slim("3.12").pip_install(["cowsay"])

# 2. Start a container with the "hi" volume mounted at /data
container = Container(
    image=image,
    name="hello-example",
    volumes=[("hi", "/data")],
)
container.run()

# 3. Run some commands
print("\n=== uname ===")
result = container.exec("uname", "-a")
print(result.stdout_text)

print("=== python version ===")
result = container.exec("python", "--version")
print(result.stdout_text)

print("=== cowsay ===")
result = container.exec("python", "-c", "import cowsay; cowsay.cow('Hello from Chalk!')")
print(result.stdout_text)

print("=== volume: ls /data ===")
result = container.exec("ls", "-la", "/data")
print(result.stdout_text)

# 4. Clean up
container.stop()
print("Done.")
