"""Call the deployed model with .remote() and .defer()/.get().

Run examples/model/deploy_trivial.py first, then:

uv run python examples/model/call.py
"""

from __future__ import annotations

import os

from chalk.client import ChalkClient

c = ChalkClient(environment=os.environ.get("CHALK_ENVIRONMENT_ID"))

MODEL_NAME = "ex-trivial-double"

model = c.get_model_version(MODEL_NAME)

y = model.remote(x=3.14)
print(f"remote: x=3.14 -> y={y}")

y = model.remote(15.9)
print(f"remote: x=15.9  -> y={y}")

xs = [2.0, 6.0, 5.0, 3.0]
handles = [model.defer(x=x) for x in xs]
print(f".defer()red {len(handles)} calls; now .get()ting the results:")
for x, h in zip(xs, handles):
    print(f"get: x={x} -> y={h.get(timeout=60)}")
