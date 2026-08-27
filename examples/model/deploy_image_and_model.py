"""Deploy a custom Image (volume-strategy code files) together with a model.

uv run python examples/model/deploy_image_and_model.py
"""

from __future__ import annotations

import json
import os
import tempfile

import pyarrow as pa
from chalk.client import ChalkClient
from chalk.scalinggroup import AutoScalingSpec, ScalingGroupResourceRequest

from chalkcompute import Image

c = ChalkClient(environment=os.environ.get("CHALK_ENVIRONMENT_ID"))

MODEL_NAME = "ex-combined-affine"
SG_NAME = "ex-combined-affine-sg"
EXAMPLE_DIR = os.path.dirname(__file__)

image = (
    Image.debian_slim("3.11")
    .pip_install(["chalk-remote-call-python", "pyarrow"])
    .add_local_file(
        os.path.join(EXAMPLE_DIR, "handler_combined.py"),
        "/app/handler_combined.py",
        strategy="volume",
    )
    .add_local_file(
        os.path.join(EXAMPLE_DIR, "predict_combined.py"),
        "/app/predict_combined.py",
        strategy="volume",
    )
    .env({"PYTHONPATH": "/app"})
    .workdir("/app")
    .entrypoint(
        [
            "chalk-remote-call",
            "--handler",
            "handler_combined.handler",
            "--on-startup",
            "handler_combined.on_startup",
            "--port",
            "8080",
        ]
    )
)

# Fixed basename: the model file mounts at /app/artifacts/<basename>.
with tempfile.TemporaryDirectory(prefix="ex-combined-") as weights_dir:
    weights_path = os.path.join(weights_dir, "weights.json")
    with open(weights_path, "w") as f:
        json.dump({"factor": 2.0, "bias": 1.0}, f)

    v = c.register_model_version(
        name=MODEL_NAME,
        input_schema={"x": pa.float64()},
        output_schema={"y": pa.float64()},
        model_image=image,
        model_paths=[weights_path],
    )

c.deploy_model_version_to_scaling_group(
    name=SG_NAME,
    model_name=MODEL_NAME,
    model_version=v.model_version,
    handler="handler_combined.handler",
    scaling=AutoScalingSpec(
        min_replicas=1,
        max_replicas=1,
        target_cpu_utilization_percentage=70,
    ),
    resources=ScalingGroupResourceRequest(cpu="500m", memory="512Mi"),
)
