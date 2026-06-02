"""Deploy a trivial model to a scaling group for testing.

uv run python examples/model/deploy_trivial.py
"""

from __future__ import annotations

import os

import pyarrow as pa
from chalkcompute import Image

from chalk.client import ChalkClient
from chalk.scalinggroup import AutoScalingSpec, ScalingGroupResourceRequest

c = ChalkClient(environment=os.environ.get("CHALK_ENVIRONMENT_ID"))

MODEL_NAME = "e2e-trivial-double"
SG_NAME = "e2e-trivial-double-sg"
HANDLER_PATH = os.path.join(os.path.dirname(__file__), "handler.py")

image = (
    Image.debian_slim("3.11")
    .pip_install(["chalk-remote-call-python", "pyarrow"])
    .add_local_file(HANDLER_PATH, "/app/handler.py", strategy="copy")
    .env({"PYTHONPATH": "/app"})
    .workdir("/app")
    .entrypoint(
        [
            "chalk-remote-call",
            "--handler",
            "handler.handler",
            "--port",
            "8080",
        ]
    )
)

v = c.register_model_version(
    name=MODEL_NAME,
    input_schema={"x": pa.float64()},
    output_schema={"y": pa.float64()},
    model_image=image,
)

c.deploy_model_version_to_scaling_group(
    name=SG_NAME,
    model_name=MODEL_NAME,
    model_version=v.model_version,
    handler="handler.handler",
    scaling=AutoScalingSpec(
        min_replicas=1,
        max_replicas=1,
        target_cpu_utilization_percentage=70,
    ),
    resources=ScalingGroupResourceRequest(cpu="500m", memory="512Mi"),
)
