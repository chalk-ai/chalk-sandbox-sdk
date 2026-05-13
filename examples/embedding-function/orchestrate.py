import logging
import os

import chalkcompute


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

EMBED_FUNCTION_NAME = os.getenv("CHALK_EMBED_FUNCTION_NAME", "embed")
FUNCTION_NAME = os.getenv("CHALK_ORCHESTRATE_FUNCTION_NAME", "orchestrate")


def _with_function_name(func):
    func.__name__ = FUNCTION_NAME
    globals()[FUNCTION_NAME] = func
    return func


@chalkcompute.function(
    image=chalkcompute.Image.debian_slim().pip_install(["chalkcompute==1.1.1"]),
    env={
        "CHALK_CLIENT_ID": os.getenv("CHALK_CLIENT_ID"),
        "CHALK_CLIENT_SECRET": os.getenv("CHALK_CLIENT_SECRET"),
        "CHALK_ENVIRONMENT_ID": os.getenv("CHALK_ENVIRONMENT_ID"),
        "CHALK_API_SERVER": os.getenv("CHALK_API_SERVER"),
        "CHALK_EMBED_FUNCTION_NAME": EMBED_FUNCTION_NAME,
        "CHALK_ORCHESTRATE_FUNCTION_NAME": FUNCTION_NAME,
    },
)
@_with_function_name
def orchestrate(texts: list[str]) -> list[list[float]]:
    import chalkcompute

    embed = chalkcompute.RemoteFunction.from_name(EMBED_FUNCTION_NAME)
    embeddings = [embed(text) for text in texts]
    return embeddings
