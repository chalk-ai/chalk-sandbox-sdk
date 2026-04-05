"""Script that runs inside the container.

Creates a trivial pyarrow Table, wraps it in a chalkdf DataFrame,
and projects a derived column.
"""

import pyarrow as pa
from chalkdf import DataFrame

table = pa.table({
    "user_id": [1, 2, 3],
    "distance_km": [0.5, 1.2, 3.7],
    "active": [True, False, True],
})

df = DataFrame(table)
print("Input:")
print(df)

projected = df.select("user_id", "distance_km").run()
print("\nProjected (user_id, distance_km):")
print(projected)
