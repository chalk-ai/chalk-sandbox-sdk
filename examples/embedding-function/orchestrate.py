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


@chalkcompute.function(
    name=make_function_name("orchestrate"),
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

    embed = chalkcompute.RemoteFunction.from_name(name=make_function_name("embed"))
    embeddings = [embed(text) for text in texts]
    return embeddings
