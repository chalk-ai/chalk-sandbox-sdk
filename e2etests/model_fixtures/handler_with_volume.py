import json

import pyarrow as pa
import pyarrow.compute as pc

config = None


def on_startup():
    global config
    with open("/app/artifacts/config.json") as f:
        config = json.load(f)


def handler(event: dict[str, pa.Array], context: dict) -> pa.Array:
    factor = config["factor"]
    return pc.multiply(event["x"], pa.scalar(factor, type=pa.float64()))
