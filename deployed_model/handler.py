"""Model handler for the DeployedModel.remote() smoke test.

Computes ``a - b`` per row. Subtraction is order-sensitive on purpose: it makes
positional argument order (and keyword reordering) observable in the output.

At deploy time the server sets ``CHALK_INPUT_ARGS="a,b"`` from the registered
input schema, so the ``chalk-remote-call`` runtime renames the incoming
positional columns ("0", "1") to "a"/"b" before calling this handler — which is
why reading ``event["a"]`` / ``event["b"]`` works.
"""

import pyarrow as pa
import pyarrow.compute as pc


def handler(event: "dict[str, pa.Array]", context: dict) -> pa.Array:
    return pc.subtract(event["a"], event["b"])
