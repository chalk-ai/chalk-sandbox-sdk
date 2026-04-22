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

EMBEDDING_MODEL = "text-embedding-3-small"


@chalkcompute.function(
    name=make_function_name("async-embed"),
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
