import json

import pyarrow as pa

from predict_combined import apply_model

weights = None


def on_startup():
    global weights
    # weights.json arrives on the model volume mounted at /app/artifacts.
    with open("/app/artifacts/weights.json") as f:
        weights = json.load(f)


def handler(event: dict[str, pa.Array], context: dict) -> pa.Array:
    return apply_model(event["x"], weights)
