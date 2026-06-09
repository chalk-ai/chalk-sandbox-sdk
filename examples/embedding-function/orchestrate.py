import logging

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


@chalkcompute.function(
    name="ex-orchestrate",
    image=chalkcompute.Image.debian_slim().pip_install(["chalkcompute>=2.1.0"]),
)
def orchestrate(texts: list[str]) -> list[list[float]]:
    import chalkcompute

    embed = chalkcompute.RemoteFunction.from_name("ex-embed")
    embeddings = [embed.remote(text) for text in texts]
    return embeddings
