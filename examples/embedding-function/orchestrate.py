import logging
import os

import chalkcompute


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


@chalkcompute.function(
    name="chalk-sandbox-test-orchestrate",
    image=chalkcompute.Image.debian_slim().pip_install(["chalkcompute==1.1.1"]),
    env={
        "CHALK_CLIENT_ID": os.getenv("CHALK_CLIENT_ID"),
        "CHALK_CLIENT_SECRET": os.getenv("CHALK_CLIENT_SECRET"),
        "CHALK_ENVIRONMENT_ID": os.getenv("CHALK_ENVIRONMENT_ID"),
        "CHALK_API_SERVER": os.getenv("CHALK_API_SERVER"),
    },
)
def orchestrate(texts: list[str]) -> list[list[float]]:
    import chalkcompute

    embed = chalkcompute.RemoteFunction.from_name(name="chalk-sandbox-test-embed")
    embeddings = [embed(text) for text in texts]
    return embeddings
