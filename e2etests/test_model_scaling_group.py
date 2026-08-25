"""End-to-end tests for model deployment to scaling groups.

Exercises:
  - Building an image via chalkcompute.Image
  - Registering a model version with that image
  - Deploying to a scaling group
  - Calling the deployed model over gRPC and verifying output
  - Uploading model artifacts to a volume and loading at runtime
"""

from __future__ import annotations

import time

import pyarrow as pa
from pyarrow import ipc

from chalk.client import ChalkClient
from chalkcompute._connect import grpc_target_from_url
from chalkcompute._remote_call_client import RemoteCallClient

from e2etests.conftest import (
    CHALK_ENVIRONMENT_ID,
    _deploy_script,
    _get_access_token,
    needs_staging,
)

SG_NAME = "ex-trivial-double-sg"
VOLUME_SG_NAME = "ex-volume-triple-sg"
HANDLER_SG_NAME = "ex-handler-affine-sg"
COMBINED_SG_NAME = "ex-combined-affine-sg"
READINESS_TIMEOUT = 180
READINESS_POLL_INTERVAL = 5


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
        _deploy_script("model", "deploy_trivial.py")
        client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)
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
        _deploy_script("model", "deploy_volume.py")
        client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)
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
        _deploy_script("model", "deploy_handler.py")
        client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)
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
        _deploy_script("model", "deploy_image_and_model.py")
        client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)
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
