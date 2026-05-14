import logging
import hashlib

import chalkcompute


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

EMBEDDING_DIMS = 1536
EMBEDDING_MODEL = "deterministic-fake-embedding"


# Example of calling in examples/batched-embedding/call.py


@chalkcompute.function(image=chalkcompute.Image.debian_slim())
def embed(text: str) -> list[float]:
    log.info("Embedding text (len=%s, model=%s)", len(text), EMBEDDING_MODEL)
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big")
    embedding: list[float] = []
    for _ in range(EMBEDDING_DIMS):
        value = (1103515245 * value + 12345) & 0x7FFFFFFF
        embedding.append((value / 0x7FFFFFFF) * 2.0 - 1.0)
    return embedding
