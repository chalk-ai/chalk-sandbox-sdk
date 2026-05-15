import logging
import os

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


@chalkcompute.function(
    image=chalkcompute.Image.debian_slim().pip_install(["chalkcompute"]),
    name="orchestrate-test",
)
def orchestrate(texts: list[str]) -> list[list[float]]:
    import chalkcompute

    embed = chalkcompute.RemoteFunction.from_name("rene-test-embed")
    embeddings = [embed(text) for text in texts]
    return embeddings
