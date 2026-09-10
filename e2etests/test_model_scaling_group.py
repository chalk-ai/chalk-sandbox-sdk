"""End-to-end tests for model deployment to scaling groups.

Exercises:
  - Building an image via chalkcompute.Image
  - Registering a model version with that image
  - Deploying to a scaling group
  - Calling the deployed model over gRPC and verifying output
  - Uploading model artifacts to a volume and loading at runtime
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import pyarrow as pa
from pyarrow import ipc

from chalk.client import ChalkClient
from chalk.client.model_image import chalk_handler_volume_name, upload_model_to_volume
from chalk.scalinggroup import AutoScalingSpec, ScalingGroupResourceRequest
from chalkcompute import Image
from chalkcompute._connect import grpc_target_from_url
from chalkcompute._remote_call_client import RemoteCallClient

from e2etests.conftest import (
    CHALK_ENVIRONMENT_ID,
    _get_access_token,
    needs_staging,
)
from e2etests.model_fixtures.handler_decorated import AffineHandler

SG_NAME = "ex-trivial-double-sg"
VOLUME_SG_NAME = "ex-volume-triple-sg"
HANDLER_SG_NAME = "ex-handler-affine-sg"
COMBINED_SG_NAME = "ex-combined-affine-sg"
READINESS_TIMEOUT = 180
READINESS_POLL_INTERVAL = 5
SCALING_GROUP_CPU = "500m"
SCALING_GROUP_MEMORY = "1Gi"
FIXTURES_DIR = Path(__file__).parent / "model_fixtures"


def _wait_for_ready_replicas(client: ChalkClient, sg_name: str, timeout: float) -> str:
    """Poll until the scaling group has at least one ready replica and the gRPC handler is serving."""
    deadline = time.monotonic() + timeout
    web_url: str | None = None
    while time.monotonic() < deadline:
        if web_url is None:
            groups = client.list_scaling_groups()
            for sg in groups.scalingGroups:
                if sg.name == sg_name and sg.readyReplicas and sg.readyReplicas > 0:
                    assert sg.webUrl, (
                        f"Scaling group {sg_name} ready but missing webUrl"
                    )
                    web_url = sg.webUrl
                    break
        if web_url is not None:
            try:
                _call_model(web_url, {"x": [1.0]})
                return web_url
            except RuntimeError:
                pass
        time.sleep(READINESS_POLL_INTERVAL)
    raise TimeoutError(f"Scaling group {sg_name} not ready after {timeout}s")


def _call_model(web_url: str, input_data: dict[str, list]) -> list[float]:
    """Send a request to the deployed model and return the first output column as a list."""
    arrays = {k: pa.array(v) for k, v in input_data.items()}
    batch = pa.record_batch(list(arrays.values()), names=list(arrays.keys()))

    sink = pa.BufferOutputStream()
    writer = ipc.new_stream(sink, batch.schema)
    writer.write_batch(batch)
    writer.close()
    feather_bytes = sink.getvalue().to_pybytes()

    target, use_tls = grpc_target_from_url(web_url)
    token = _get_access_token()
    client = RemoteCallClient(
        target,
        metadata=[
            ("authorization", f"Bearer {token}"),
            ("x-chalk-env-id", CHALK_ENVIRONMENT_ID),
        ],
        use_tls=use_tls,
    )
    chunks = client.call_ipc("handler", feather_bytes)
    assert len(chunks) > 0, "No response from model"

    reader = ipc.open_stream(chunks[0])
    batches = list(reader)
    assert len(batches) > 0, "No batches in response"

    result = batches[0]
    assert result.num_columns > 0, "Response has no columns"
    return result.column(0).to_pylist()


@needs_staging
class TestModelScalingGroup:
    _web_url: str

    @classmethod
    def setup_class(cls):
        client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)
        image = (
            Image.debian_slim("3.11")
            .pip_install(["chalk-remote-call-python", "pyarrow"])
            .add_local_file(
                str(FIXTURES_DIR / "handler.py"),
                "/app/handler.py",
                strategy="copy",
            )
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
        version = client.register_model_version(
            name="ex-trivial-double",
            input_schema={"x": pa.float64()},
            output_schema={"y": pa.float64()},
            model_image=image,
        )
        client.deploy_model_version_to_scaling_group(
            name=SG_NAME,
            model_name="ex-trivial-double",
            model_version=version.model_version,
            handler="handler.handler",
            scaling=AutoScalingSpec(
                min_replicas=1,
                max_replicas=1,
                target_cpu_utilization_percentage=70,
            ),
            resources=ScalingGroupResourceRequest(
                cpu=SCALING_GROUP_CPU, memory=SCALING_GROUP_MEMORY
            ),
        )
        cls._web_url = _wait_for_ready_replicas(client, SG_NAME, READINESS_TIMEOUT)

    @classmethod
    def teardown_class(cls):
        try:
            client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)
            client.delete_scaling_group(name=SG_NAME)
        except Exception:
            pass

    def test_single_input(self):
        """handler(x=3.0) should return 6.0."""
        result = _call_model(self._web_url, {"x": [3.0]})
        assert result == [6.0], f"Expected [6.0], got {result}"

    def test_batch_input(self):
        """handler should double each element in a batch."""
        result = _call_model(self._web_url, {"x": [1.0, 2.0, 5.0, 10.0]})
        expected = [2.0, 4.0, 10.0, 20.0]
        assert result == expected, f"Expected {expected}, got {result}"

    def test_zero_and_negative(self):
        """handler should handle zero and negative values."""
        result = _call_model(self._web_url, {"x": [0.0, -3.0, -0.5]})
        expected = [0.0, -6.0, -1.0]
        assert result == expected, f"Expected {expected}, got {result}"


@needs_staging
class TestModelWithVolume:
    """Tests model deployment with a volume-mounted config.

    The handler loads a config.json from /app/artifacts (the canonical
    volume mount path) at startup and uses its "factor" field to multiply
    inputs. This validates the full volume flow: upload_model_to_volume
    with chalk_handler_volume_name, automatic volume detection in
    _ensure_model_image, and runtime loading from the mount.
    """

    _web_url: str

    @classmethod
    def setup_class(cls):
        client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)
        image = (
            Image.debian_slim("3.11")
            .pip_install(["chalk-remote-call-python", "pyarrow"])
            .add_local_file(
                str(FIXTURES_DIR / "handler_with_volume.py"),
                "/app/handler.py",
                strategy="copy",
            )
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
        version = client.register_model_version(
            name="ex-volume-triple",
            input_schema={"x": pa.float64()},
            output_schema={"y": pa.float64()},
            model_image=image,
        )
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w") as config_file:
            json.dump({"factor": 3.0}, config_file)
            config_file.flush()
            upload_model_to_volume(
                volume_name=chalk_handler_volume_name(
                    "ex-volume-triple", version.model_version
                ),
                model_filename="config.json",
                model_file_path=config_file.name,
            )
        client.deploy_model_version_to_scaling_group(
            name=VOLUME_SG_NAME,
            model_name="ex-volume-triple",
            model_version=version.model_version,
            handler="handler.handler",
            scaling=AutoScalingSpec(
                min_replicas=1,
                max_replicas=1,
                target_cpu_utilization_percentage=70,
            ),
            resources=ScalingGroupResourceRequest(
                cpu=SCALING_GROUP_CPU, memory=SCALING_GROUP_MEMORY
            ),
        )
        cls._web_url = _wait_for_ready_replicas(
            client, VOLUME_SG_NAME, READINESS_TIMEOUT
        )

    @classmethod
    def teardown_class(cls):
        try:
            client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)
            client.delete_scaling_group(name=VOLUME_SG_NAME)
        except Exception:
            pass

    def test_single_input(self):
        """handler(x=4.0) with factor=3.0 should return 12.0."""
        result = _call_model(self._web_url, {"x": [4.0]})
        assert result == [12.0], f"Expected [12.0], got {result}"

    def test_batch_input(self):
        """handler should triple each element in a batch."""
        result = _call_model(self._web_url, {"x": [1.0, 2.0, 5.0]})
        expected = [3.0, 6.0, 15.0]
        assert result == expected, f"Expected {expected}, got {result}"


@needs_staging
class TestModelHandlerDecorator:
    """Tests the @model_handler registration path.

    chalkpy builds the serving image from the decorated class, uploads
    files= to the model artifact volume, and serves via the baked shim
    (no handler= passed at deploy). The handler loads weights.json from
    the volume, so a passing call proves the volume had content.
    """

    _web_url: str

    @classmethod
    def setup_class(cls):
        client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)
        with tempfile.TemporaryDirectory(prefix="model-handler-") as weights_dir:
            weights_path = os.path.join(weights_dir, "weights.json")
            with open(weights_path, "w") as weights_file:
                json.dump({"factor": 5.0, "bias": 2.0}, weights_file)
            version = client.register_model_version(
                name="ex-handler-affine",
                model=AffineHandler(files=[weights_path]),
                input_schema={"x": pa.float64()},
                output_schema={"y": pa.float64()},
            )
        client.deploy_model_version_to_scaling_group(
            name=HANDLER_SG_NAME,
            model_name="ex-handler-affine",
            model_version=version.model_version,
            scaling=AutoScalingSpec(
                min_replicas=1,
                max_replicas=1,
                target_cpu_utilization_percentage=70,
            ),
            resources=ScalingGroupResourceRequest(
                cpu=SCALING_GROUP_CPU, memory=SCALING_GROUP_MEMORY
            ),
        )
        cls._web_url = _wait_for_ready_replicas(
            client, HANDLER_SG_NAME, READINESS_TIMEOUT
        )

    @classmethod
    def teardown_class(cls):
        try:
            client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)
            client.delete_scaling_group(name=HANDLER_SG_NAME)
        except Exception:
            pass

    def test_single_input(self):
        """predict(x=3.0) with factor=5.0, bias=2.0 should return 17.0."""
        result = _call_model(self._web_url, {"x": [3.0]})
        assert result == [17.0], f"Expected [17.0], got {result}"

    def test_batch_input(self):
        """predict should compute 5x + 2 for each element in a batch."""
        result = _call_model(self._web_url, {"x": [0.0, 1.0, -2.0]})
        expected = [2.0, 7.0, -8.0]
        assert result == expected, f"Expected {expected}, got {result}"


@needs_staging
class TestModelImageAndModelCombined:
    """Tests registering a custom image together with model_paths.

    The image carries code files with strategy="volume" and the model
    weights ride the model artifact volume, so both volumes must be
    attached at deploy. The handler computes 2x + 1 from weights.json.
    """

    _web_url: str

    @classmethod
    def setup_class(cls):
        client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)
        image = (
            Image.debian_slim("3.11")
            .pip_install(["chalk-remote-call-python", "pyarrow"])
            .add_local_file(
                str(FIXTURES_DIR / "handler_combined.py"),
                "/app/handler_combined.py",
                strategy="volume",
            )
            .add_local_file(
                str(FIXTURES_DIR / "predict_combined.py"),
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
        with tempfile.TemporaryDirectory(prefix="model-combined-") as weights_dir:
            weights_path = os.path.join(weights_dir, "weights.json")
            with open(weights_path, "w") as weights_file:
                json.dump({"factor": 2.0, "bias": 1.0}, weights_file)
            version = client.register_model_version(
                name="ex-combined-affine",
                input_schema={"x": pa.float64()},
                output_schema={"y": pa.float64()},
                model_image=image,
                model_paths=[weights_path],
            )
        client.deploy_model_version_to_scaling_group(
            name=COMBINED_SG_NAME,
            model_name="ex-combined-affine",
            model_version=version.model_version,
            handler="handler_combined.handler",
            scaling=AutoScalingSpec(
                min_replicas=1,
                max_replicas=1,
                target_cpu_utilization_percentage=70,
            ),
            resources=ScalingGroupResourceRequest(
                cpu=SCALING_GROUP_CPU, memory=SCALING_GROUP_MEMORY
            ),
        )
        cls._web_url = _wait_for_ready_replicas(
            client, COMBINED_SG_NAME, READINESS_TIMEOUT
        )

    @classmethod
    def teardown_class(cls):
        try:
            client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)
            client.delete_scaling_group(name=COMBINED_SG_NAME)
        except Exception:
            pass

    def test_single_input(self):
        """handler(x=3.0) with factor=2.0, bias=1.0 should return 7.0."""
        result = _call_model(self._web_url, {"x": [3.0]})
        assert result == [7.0], f"Expected [7.0], got {result}"

    def test_batch_input(self):
        """handler should compute 2x + 1 for each element in a batch."""
        result = _call_model(self._web_url, {"x": [0.0, 1.0, -2.0]})
        expected = [1.0, 3.0, -3.0]
        assert result == expected, f"Expected {expected}, got {result}"
