"""Deploy a model that loads config from image-baked artifacts.

The handler reads ``/app/artifacts/config.json`` at startup and uses
its ``"factor"`` field to multiply inputs.  The config file is baked
into the image via ``add_local_file`` rather than uploaded to a
separate volume.

uv run python examples/model/deploy_volume.py
"""

from __future__ import annotations

import json
import os
import tempfile

import pyarrow as pa
from chalkcompute import Image

from chalk.client import ChalkClient
from chalk.scalinggroup import AutoScalingSpec, ScalingGroupResourceRequest

c = ChalkClient(environment=os.environ.get("CHALK_ENVIRONMENT_ID"))

MODEL_NAME = "ex-volume-triple"
SG_NAME = "ex-volume-triple-sg"
HANDLER_PATH = os.path.join(os.path.dirname(__file__), "handler_with_volume.py")

config_tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
json.dump({"factor": 3.0}, config_tmp)
config_tmp.close()

image = (
    Image.debian_slim("3.11")
    .pip_install(["chalk-remote-call-python", "pyarrow"])
    .add_local_file(HANDLER_PATH, "/app/handler.py", strategy="copy")
    .add_local_file(config_tmp.name, "/app/artifacts/config.json", strategy="copy")
    .env({"PYTHONPATH": "/app"})
    .workdir("/app")
    .entrypoint(
        [
            "chalk-remote-call",
            "--handler",
            "handler.handler",
            "--on-startup",
            "handler.on_startup",
            "--port",
            "8080",
        ]
    )
)

os.unlink(config_tmp.name)

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
