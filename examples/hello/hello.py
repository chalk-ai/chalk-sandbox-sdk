#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["chalkcompute>=2.11.1"]
# ///
"""Start a Debian sandbox, run a few commands, then terminate it."""

from __future__ import annotations

from chalkcompute import Image, SandboxClient

# 1. Define an image: Python slim + cowsay
image = Image.debian_slim().pip_install(["cowsay"])

with SandboxClient.from_env() as client:
    sandbox = client.create(image, lifetime="600s")
    try:
        print("\n=== uname ===")
        print(sandbox.exec("uname", "-a").stdout_text)

        print("=== python version ===")
        print(sandbox.exec("python", "--version").stdout_text)

        print("=== cowsay ===")
        result = sandbox.exec(
            "python", "-c", "import cowsay; cowsay.cow('Hello from Chalk!')"
        )
        print(result.stdout_text)
    finally:
        sandbox.terminate()

print("Done.")
