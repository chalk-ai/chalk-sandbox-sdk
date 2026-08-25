"""Deploy a model via the @model_handler decorator.

uv run python examples/model/deploy_handler.py
"""

from __future__ import annotations

import json
import os
import tempfile

import pyarrow as pa
from handler_decorated import AffineHandler

from chalk.client import ChalkClient
from chalk.scalinggroup import AutoScalingSpec, ScalingGroupResourceRequest

c = ChalkClient(environment=os.environ.get("CHALK_ENVIRONMENT_ID"))

MODEL_NAME = "ex-handler-affine"
SG_NAME = "ex-handler-affine-sg"

with tempfile.TemporaryDirectory(prefix="ex-handler-") as weights_dir:
    weights_path = os.path.join(weights_dir, "weights.json")
    with open(weights_path, "w") as f:
        json.dump({"factor": 5.0, "bias": 2.0}, f)

    v = c.register_model_version(
        name=MODEL_NAME,
        model=AffineHandler(files=[weights_path]),
        input_schema={"x": pa.float64()},
        output_schema={"y": pa.float64()},
    )

c.deploy_model_version_to_scaling_group(
    name=SG_NAME,
    model_name=MODEL_NAME,
    model_version=v.model_version,
    scaling=AutoScalingSpec(
        min_replicas=1,
        max_replicas=1,
        target_cpu_utilization_percentage=70,
    ),
    resources=ScalingGroupResourceRequest(cpu="500m", memory="1Gi"),
)
