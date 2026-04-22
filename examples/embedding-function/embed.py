import logging
import os

import chalkcompute


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"


# Example of calling in examples/batched-embedding/call.py


@chalkcompute.function(
    name="chalk-sandbox-test-embed",
    image=chalkcompute.Image.debian_slim().pip_install(["openai"]),
    env={
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
    },
)
def embed(text: str) -> list[float]:
    from openai import OpenAI

    log.info("Embedding text (len=%s, model=%s)", len(text), EMBEDDING_MODEL)
    client = OpenAI()
    response = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return list(response.data[0].embedding)
