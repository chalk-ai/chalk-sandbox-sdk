import argparse
import logging
import os

import chalkcompute


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy async embedding function")
    parser.add_argument(
        "--function-name",
        default="async-embed",
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
    async def async_embed(text: str) -> list[float]:
        from openai import AsyncOpenAI

        log.info("Embedding text async (len=%s, model=%s)", len(text), EMBEDDING_MODEL)
        client = AsyncOpenAI()
        response = await client.embeddings.create(model=EMBEDDING_MODEL, input=text)
        return list(response.data[0].embedding)


if __name__ == "__main__":
    main()
