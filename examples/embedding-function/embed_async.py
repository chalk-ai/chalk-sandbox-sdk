import logging
import os

import chalkcompute


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"


@chalkcompute.function(
    name="async-embed",
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
