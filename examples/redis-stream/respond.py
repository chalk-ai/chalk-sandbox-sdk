#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""Deploy a chalkcompute function that uses OpenAI to respond to a prompt.

This deployment intentionally remains available when this file is executed
directly so independently managed stream workers can call it. The end-to-end
``run.py`` example owns its deployment and deletes it during cleanup.
"""

import logging
import os

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

MODEL = "gpt-4o-mini"
FUNCTION_NAME = os.environ.get("REDIS_FUNCTION_NAME", "ex-redis-respond")


@chalkcompute.function(
    name=FUNCTION_NAME,
    image=chalkcompute.Image.debian_slim().pip_install(["openai"]),
    env={"OPENAI_API_KEY": chalkcompute.Secret.from_local_env("OPENAI_API_KEY")},
)
def respond(text: str) -> str:
    from openai import OpenAI

    log.info("Generating response for text (len=%d, model=%s)", len(text), MODEL)
    client = OpenAI()
    completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": "You are a helpful assistant. Respond concisely.",
            },
            {"role": "user", "content": text},
        ],
        max_tokens=256,
    )
    return completion.choices[0].message.content or ""


if __name__ == "__main__":
    respond.wait_ready(timeout=600)
    print(f"Deployed {FUNCTION_NAME}")
