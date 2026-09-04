import pyarrow as pa
import pyarrow.compute as pc


def apply_model(x: pa.Array, weights: dict) -> pa.Array:
    return pc.add(pc.multiply(x, weights["factor"]), weights["bias"])
