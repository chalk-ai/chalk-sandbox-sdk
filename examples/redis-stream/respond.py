"""Deploy a chalkcompute function that uses OpenAI to respond to a prompt.

Run this file to deploy the function:

    uv run python examples/redis-stream/respond.py
"""

import logging
import os
import sys
from pathlib import Path

import chalkcompute

# Add examples dir to path so we can import test_utils
sys.path.insert(0, str(Path(__file__).parent.parent))
from test_utils import make_function_name

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

MODEL = "gpt-4o-mini"


@chalkcompute.function(
    name=make_function_name("respond"),
    image=chalkcompute.Image.debian_slim().pip_install(["openai"]),
    env={
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
    },
)
def respond(text: str) -> str:
    from openai import OpenAI

    log.info("Generating response for text (len=%d, model=%s)", len(text), MODEL)
    client = OpenAI()
    completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "You are a helpful assistant. Respond concisely."},
            {"role": "user", "content": text},
        ],
        max_tokens=256,
    )
    return completion.choices[0].message.content or ""
