import pyarrow as pa
import pyarrow.compute as pc


def handler(event: dict[str, pa.Array], context: dict) -> pa.Array:
    return pc.multiply(event["x"], pa.scalar(2.0, type=pa.float64()))
