import chalkcompute
from chalkcompute import Image

import shared_utils
from legacy import scoring
from fraud.helpers import double


@chalkcompute.function(
    name="ex-fraud-fn",
    image=Image.debian_slim().add_local_python_source("shared_utils", "legacy"),
)
def fraud_fn(x: int) -> int:
    return scoring.score(shared_utils.clean(double(x)))
