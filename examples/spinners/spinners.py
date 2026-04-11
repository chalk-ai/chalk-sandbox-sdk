#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["chalkcompute", "python-dotenv", "rich"]
# ///

# # Rich Spinners Demo
#
# Demonstrates the hierarchical progress display that activates when
# `CHALK_COMPUTE_SPINNERS=1` is set.  The display shows each phase of
# `Container.run()` — volume creation, file upload, image check/build,
# container boot, and ChalkFS file sync — as a tree of spinners that
# resolve to checkmarks as each step completes.
#
# ## What happens
#
# 1. We build an `Image` with a local file added via the default
#    `strategy="volume"` path, which triggers the full volume → upload →
#    image → boot → sync pipeline.
# 2. `Container.run()` drives the progress display automatically.
# 3. We exec a command to prove the file arrived, then clean up.
#
# ## Running
#
# ```bash
# CHALK_COMPUTE_SPINNERS=1 python examples/spinners/spinners.py
# ```
#
# Without the env var, you get the normal plain-text output instead.

import os
import sys
import tempfile
import uuid
from pathlib import Path

from dotenv import load_dotenv

# Load credentials from the repo-root .env (decrypted via `just env`).
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from chalkcompute import Container, Image


def main() -> None:
    # ## Build the image
    #
    # A minimal Debian slim image with one local file mounted via a Chalk
    # volume.  This exercises every progress step: volume creation, file
    # upload, image check, and (on first run) image build.

    suffix = uuid.uuid4().hex[:8]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("Hello from the spinners demo!\n")
        local_file = f.name

    try:
        image = (
            Image.debian_slim("3.12")
            .add_local_file(local_file, "/app/hello.txt")
        )

        # ## Launch the container
        #
        # With `CHALK_COMPUTE_SPINNERS=1`, the `.run()` call renders a live
        # tree of spinners in the terminal.  Each step transitions from
        # `◌ pending` → `⠸ in progress` → `✓ done`.

        container = Container(
            image=image,
            name=f"spinners-demo-{suffix}",
            lifetime="5m",
        ).run()

        # ## Verify the file landed
        result = container.exec("cat", "/app/hello.txt")
        print(f"\n--- exec output ---\n{result.stdout_text.strip()}")

    finally:
        os.unlink(local_file)
        # ## Clean up
        try:
            container.stop()  # type: ignore[possibly-unbound]
            print("Container stopped.")
        except Exception:
            pass


if __name__ == "__main__":
    main()
