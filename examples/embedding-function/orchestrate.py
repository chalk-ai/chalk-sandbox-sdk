import logging
import os

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

EMBED_FUNCTION_NAME = os.environ.get("CHALK_E2E_EMBED_FUNCTION_NAME", "ex-embed")


@chalkcompute.function(
    name=os.environ.get("CHALK_E2E_FUNCTION_NAME", "ex-orchestrate"),
    image=chalkcompute.Image.debian_slim().pip_install(["chalkcompute>=2.1.0"]),
    env={"CHALK_E2E_EMBED_FUNCTION_NAME": EMBED_FUNCTION_NAME},
)
def orchestrate(texts: list[str]) -> list[list[float]]:
    import chalkcompute

    embed = chalkcompute.RemoteFunction.from_name(EMBED_FUNCTION_NAME)
    embeddings = [embed.remote(text) for text in texts]
    return embeddings
