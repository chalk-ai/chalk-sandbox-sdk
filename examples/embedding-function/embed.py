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


# Example of calling in examples/batched-embedding/call.py


@chalkcompute.function(
    name=make_function_name("embed"),
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
