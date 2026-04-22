"""Deploy a chalkcompute function that uses OpenAI to respond to a prompt.

Run this file to deploy the function:

    uv run python examples/redis-stream/respond.py

Optional: pass --function-name to customize the deployed function name.
"""

import argparse
import logging
import os

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

MODEL = "gpt-4o-mini"


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy OpenAI respond function")
    parser.add_argument(
        "--function-name",
        default="chalk-sandbox-test-respond",
        help="Name for the deployed function",
    )
    args = parser.parse_args()

    @chalkcompute.function(
        name=args.function_name,
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


if __name__ == "__main__":
    main()
