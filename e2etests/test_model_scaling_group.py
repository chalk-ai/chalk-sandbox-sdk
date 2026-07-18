"""End-to-end tests for model deployment to scaling groups.

Exercises the documented model-deployment workflow:
  1. Register a model version (``@model_handler`` or container image)
  2. Deploy to a scaling group with ``deploy_model_version_to_scaling_group``
  3. Call the deployed model over gRPC and verify output
"""

from __future__ import annotations

import time

import pyarrow as pa
from pyarrow import ipc

from chalk.client import ChalkClient
from chalk.ml.model_handler import model_handler
from chalk.scalinggroup import AutoScalingSpec, ScalingGroupResourceRequest
from chalkcompute._connect import grpc_target_from_url
from chalkcompute._remote_call_client import RemoteCallClient

from e2etests.conftest import (
    CHALK_ENVIRONMENT_ID,
    _get_access_token,
    needs_staging,
)

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


# ---------------------------------------------------------------------------
# @model_handler classes — defined at module scope so they're picklable
# ---------------------------------------------------------------------------


@model_handler
class DoublingModel:
    """Multiplies input ``x`` by 2.0 using a trained linear model."""

    def predict(self, df):
        import pandas as pd

        return pd.DataFrame({"y": self.model.predict(df.to_pandas())})


@model_handler
class TriplingModel:
    """Multiplies input ``x`` by 3.0 using a trained linear model."""

    def predict(self, df):
        import pandas as pd

        return pd.DataFrame({"y": self.model.predict(df.to_pandas())})


@needs_staging
class TestModelScalingGroup:
    """Tests @model_handler deployment: a linear model that doubles inputs."""

    _web_url: str

    @classmethod
    def setup_class(cls):
        client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)

        import numpy as np
        from sklearn.linear_model import LinearRegression

        X_train = np.array([[1.0], [2.0], [3.0], [4.0], [5.0]])
        y_train = X_train.ravel() * 2.0

        model = LinearRegression().fit(X_train, y_train)

        result = client.register_model_version(
            name="ex-trivial-double",
            model=DoublingModel(model=model),
            input_schema={"x": float},
            output_schema={"y": float},
            dependencies=["scikit-learn", "pandas"],
        )

        client.deploy_model_version_to_scaling_group(
            name="ex-trivial-double-sg",
            model_name="ex-trivial-double",
            model_version=result.model_version,
            scaling=AutoScalingSpec(
                min_replicas=1,
                max_replicas=1,
                target_cpu_utilization_percentage=70,
            ),
            resources=ScalingGroupResourceRequest(cpu="500m", memory="512Mi"),
        )

        cls._web_url = _wait_for_ready_replicas(
            client, "ex-trivial-double-sg", READINESS_TIMEOUT
        )

    @classmethod
    def teardown_class(cls):
        try:
            client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)
            client.delete_scaling_group(name="ex-trivial-double-sg")
        except Exception:
            pass

    def test_single_input(self):
        """model.predict(x=3.0) should return ~6.0."""
        result = _call_model(self._web_url, {"x": [3.0]})
        assert len(result) == 1
        assert abs(result[0] - 6.0) < 0.01, f"Expected ~6.0, got {result[0]}"

    def test_batch_input(self):
        """model should double each element in a batch."""
        result = _call_model(self._web_url, {"x": [1.0, 2.0, 5.0, 10.0]})
        expected = [2.0, 4.0, 10.0, 20.0]
        for got, exp in zip(result, expected):
            assert abs(got - exp) < 0.01, f"Expected ~{exp}, got {got}"

    def test_zero_and_negative(self):
        """model should handle zero and negative values."""
        result = _call_model(self._web_url, {"x": [0.0, -3.0, -0.5]})
        expected = [0.0, -6.0, -1.0]
        for got, exp in zip(result, expected):
            assert abs(got - exp) < 0.01, f"Expected ~{exp}, got {got}"


@needs_staging
class TestModelWithArtifacts:
    """Tests @model_handler deployment with a tripling linear model.

    Validates that model serialization, artifact upload, and runtime
    deserialization via self.model work end-to-end.
    """

    _web_url: str

    @classmethod
    def setup_class(cls):
        client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)

        import numpy as np
        from sklearn.linear_model import LinearRegression

        X_train = np.array([[1.0], [2.0], [3.0], [4.0], [5.0]])
        y_train = X_train.ravel() * 3.0

        model = LinearRegression().fit(X_train, y_train)

        result = client.register_model_version(
            name="ex-volume-triple",
            model=TriplingModel(model=model),
            input_schema={"x": float},
            output_schema={"y": float},
            dependencies=["scikit-learn", "pandas"],
        )

        client.deploy_model_version_to_scaling_group(
            name="ex-volume-triple-sg",
            model_name="ex-volume-triple",
            model_version=result.model_version,
            scaling=AutoScalingSpec(
                min_replicas=1,
                max_replicas=1,
                target_cpu_utilization_percentage=70,
            ),
            resources=ScalingGroupResourceRequest(cpu="500m", memory="512Mi"),
        )

        cls._web_url = _wait_for_ready_replicas(
            client, "ex-volume-triple-sg", READINESS_TIMEOUT
        )

    @classmethod
    def teardown_class(cls):
        try:
            client = ChalkClient(environment=CHALK_ENVIRONMENT_ID)
            client.delete_scaling_group(name="ex-volume-triple-sg")
        except Exception:
            pass

    def test_single_input(self):
        """model.predict(x=4.0) with factor=3.0 should return ~12.0."""
        result = _call_model(self._web_url, {"x": [4.0]})
        assert len(result) == 1
        assert abs(result[0] - 12.0) < 0.01, f"Expected ~12.0, got {result[0]}"

    def test_batch_input(self):
        """model should triple each element in a batch."""
        result = _call_model(self._web_url, {"x": [1.0, 2.0, 5.0]})
        expected = [3.0, 6.0, 15.0]
        for got, exp in zip(result, expected):
            assert abs(got - exp) < 0.01, f"Expected ~{exp}, got {got}"
