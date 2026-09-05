from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from chalkcompute._containerspec import ContainerSpec
from chalkcompute._training import (
    TrainingError,
    TrainingFunction,
    TrainingRunHandle,
    _NoOpTrainingFunction,
    _config_to_struct,
    _metadata_to_proto,
    training,
)


def test_decorator_with_data_arg() -> None:
    @training(data="my_dataset")
    def train(df: Any, config: Any) -> str:
        return "ok"

    assert isinstance(train, TrainingFunction)
    assert train.func_name == "train"
    assert train.data_source == "my_dataset"
    assert train.data_source_type == "dataset_name"


def test_decorator_with_dataset_arg() -> None:
    @training(dataset="my_dataset")
    def train(df: Any, config: Any) -> str:
        return "ok"

    assert isinstance(train, TrainingFunction)
    assert train.data_source == "my_dataset"
    assert train.data_source_type == "dataset_name"


def test_decorator_with_input_sql() -> None:
    @training(input_sql="SELECT * FROM features")
    def train(df: Any, config: Any) -> str:
        return "ok"

    assert isinstance(train, TrainingFunction)
    assert train.data_source == "SELECT * FROM features"
    assert train.data_source_type == "input_sql"


def test_decorator_rejects_no_data_source() -> None:
    with pytest.raises(TrainingError, match="exactly one"):

        @training
        def train(df: Any, config: Any) -> str:
            return "ok"


def test_decorator_rejects_multiple_data_sources() -> None:
    with pytest.raises(TrainingError, match="only one"):

        @training(data="ds", input_sql="SELECT 1")
        def train(df: Any, config: Any) -> str:
            return "ok"


def test_call_invokes_function_body() -> None:
    @training(data="ds")
    def train(df: Any, config: Any) -> str:
        return f"got-{config}"

    assert train(None, "myconfig") == "got-myconfig"


def test_local_invokes_function_body() -> None:
    @training(data="ds")
    def train(df: Any, config: Any) -> str:
        return f"local-{config}"

    assert train.local(config="c") == "local-c"


def test_noop_inside_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHALK_TRAINING_RUN_ID", "run-123")

    @training(data="ds")
    def train(df: Any, config: Any) -> str:
        return "container"

    assert isinstance(train, _NoOpTrainingFunction)
    assert train(None, None) == "container"


def test_training_function_outside_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHALK_TRAINING_RUN_ID", raising=False)

    @training(data="ds")
    def train(df: Any, config: Any) -> str:
        return "ok"

    assert isinstance(train, TrainingFunction)


def test_decorator_with_all_options() -> None:
    @training(
        data="ds",
        name="my-run",
        cpu="8",
        memory="32Gi",
        gpu="a10g",
        env={"KEY": "VAL"},
        metadata={"team": "ml"},
    )
    def train(df: Any, config: Any) -> str:
        return "ok"

    assert isinstance(train, TrainingFunction)
    assert train._name == "my-run"
    assert train._cpu == "8"
    assert train._memory == "32Gi"
    assert train._gpu == "a10g"
    assert train._env == {"KEY": "VAL"}
    assert train._metadata == {"team": "ml"}


# --- config serialization ---


def test_config_to_struct_dict() -> None:
    s = _config_to_struct({"lr": 0.01, "epochs": 10})
    assert s is not None
    assert s.fields["lr"].kind.value == pytest.approx(0.01)
    assert s.fields["epochs"].kind.value == pytest.approx(10.0)


def test_config_to_struct_dataclass() -> None:
    @dataclass
    class Cfg:
        lr: float = 0.01
        epochs: int = 10

    s = _config_to_struct(Cfg())
    assert s is not None
    assert s.fields["lr"].kind.value == pytest.approx(0.01)


def test_config_to_struct_none() -> None:
    assert _config_to_struct(None) is None


def test_config_to_struct_rejects_invalid() -> None:
    with pytest.raises(TrainingError, match="dict or dataclass"):
        _config_to_struct("not-a-config")


# --- metadata serialization ---


def test_metadata_to_proto() -> None:
    result = _metadata_to_proto({"team": "ml", "version": 2})
    assert result["team"].kind.value == "ml"
    assert result["version"].kind.value == pytest.approx(2.0)


# --- data source proto ---


def test_build_data_source_dataset() -> None:
    @training(data="my_ds")
    def train(df: Any, config: Any) -> str:
        return "ok"

    ds = train._build_data_source()
    assert ds.source.field == "dataset_name"
    assert ds.source.value == "my_ds"


def test_build_data_source_input_sql() -> None:
    @training(input_sql="SELECT 1")
    def train(df: Any, config: Any) -> str:
        return "ok"

    ds = train._build_data_source()
    assert ds.source.field == "input_sql"
    assert ds.source.value == "SELECT 1"


# --- resources proto ---


def test_build_resources_all() -> None:
    @training(data="ds", cpu="4", memory="16Gi", gpu="a10g")
    def train(df: Any, config: Any) -> str:
        return "ok"

    r = train._build_resources()
    assert r is not None
    assert r.cpu == "4"
    assert r.memory == "16Gi"
    assert r.gpu == "a10g"


def test_build_resources_none() -> None:
    @training(data="ds")
    def train(df: Any, config: Any) -> str:
        return "ok"

    assert train._build_resources() is None


# --- TrainingRunHandle ---


def test_handle_repr() -> None:
    handle = TrainingRunHandle("run-abc", MagicMock())
    assert repr(handle) == "TrainingRunHandle(run_id='run-abc')"


def test_handle_run_id() -> None:
    handle = TrainingRunHandle("run-123", MagicMock())
    assert handle.run_id == "run-123"


def test_handle_wait_returns_self() -> None:
    from chalkcompute._gen.chalk.server.v1 import training_runs_pb as tr_pb

    mock_client = MagicMock()
    completed_run = tr_pb.TrainingRun(
        id="run-w", status=tr_pb.TrainingRunStatus.COMPLETED
    )
    mock_client.call.return_value = tr_pb.GetTrainingRunResponse(
        training_run=completed_run
    )
    handle = TrainingRunHandle("run-w", mock_client)
    result = handle.wait(poll_interval=0)
    assert result is handle


def test_handle_wait_raises_on_failed() -> None:
    from chalkcompute._gen.chalk.server.v1 import training_runs_pb as tr_pb

    mock_client = MagicMock()
    failed_run = tr_pb.TrainingRun(
        id="run-f",
        status=tr_pb.TrainingRunStatus.FAILED,
        error_message="boom",
    )
    mock_client.call.return_value = tr_pb.GetTrainingRunResponse(
        training_run=failed_run
    )
    handle = TrainingRunHandle("run-f", mock_client)
    with pytest.raises(TrainingError, match="boom"):
        handle.wait(poll_interval=0)


def test_handle_wait_raises_on_canceled() -> None:
    from chalkcompute._gen.chalk.server.v1 import training_runs_pb as tr_pb

    mock_client = MagicMock()
    canceled_run = tr_pb.TrainingRun(
        id="run-c", status=tr_pb.TrainingRunStatus.CANCELED
    )
    mock_client.call.return_value = tr_pb.GetTrainingRunResponse(
        training_run=canceled_run
    )
    handle = TrainingRunHandle("run-c", mock_client)
    with pytest.raises(TrainingError, match="canceled"):
        handle.wait(poll_interval=0)


def test_handle_wait_timeout() -> None:
    from chalkcompute._gen.chalk.server.v1 import training_runs_pb as tr_pb

    mock_client = MagicMock()
    working_run = tr_pb.TrainingRun(id="run-t", status=tr_pb.TrainingRunStatus.WORKING)
    mock_client.call.return_value = tr_pb.GetTrainingRunResponse(
        training_run=working_run
    )
    handle = TrainingRunHandle("run-t", mock_client)
    with pytest.raises(TimeoutError):
        handle.wait(poll_interval=0, timeout=0.0)


def test_handle_latest_checkpoint() -> None:
    from chalkcompute._gen.chalk.server.v1 import (
        model_registry_pb as mr_pb,
        training_runs_pb as tr_pb,
    )

    artifact = mr_pb.ModelArtifact(id="art-1", path="env/artifacts/art-1")
    mock_client = MagicMock()
    mock_client.call.return_value = tr_pb.GetLatestCheckpointResponse(
        model_artifact=artifact
    )
    handle = TrainingRunHandle("run-ck", mock_client)
    result = handle.latest_checkpoint()
    assert result is not None
    assert result.id == "art-1"
    assert result.path == "env/artifacts/art-1"


def test_handle_latest_checkpoint_none() -> None:
    from chalkcompute._gen.chalk.server.v1 import training_runs_pb as tr_pb

    mock_client = MagicMock()
    mock_client.call.return_value = tr_pb.GetLatestCheckpointResponse()
    handle = TrainingRunHandle("run-ck2", mock_client)
    assert handle.latest_checkpoint() is None


def test_handle_cancel() -> None:
    mock_client = MagicMock()
    handle = TrainingRunHandle("run-can", mock_client)
    handle.cancel()
    mock_client.call.assert_called_once()


# --- run_training RPC integration (mocked) ---


def _mock_deploy_image() -> MagicMock:
    img = MagicMock()
    img.lazy_local_files = []
    return img


def test_run_training_calls_create_rpc() -> None:
    from chalkcompute._gen.chalk.server.v1 import training_runs_pb as tr_pb

    @training(data="my_ds", name="test-run", cpu="4")
    def train(df: Any, config: Any) -> str:
        return "ok"

    mock_client = MagicMock()
    mock_run = tr_pb.TrainingRun(id="run-xyz", name="test-run")
    mock_resp = tr_pb.CreateTrainingRunResponse(training_run=mock_run)
    mock_client.call.return_value = mock_resp

    with (
        patch.object(TrainingFunction, "_build_deploy_image", return_value=_mock_deploy_image()),
        patch("chalkcompute._training.build_image", return_value="img:latest"),
    ):
        handle = train.run_training(config={"lr": 0.01}, client=mock_client)
    assert isinstance(handle, TrainingRunHandle)
    assert handle.run_id == "run-xyz"
    mock_client.call.assert_called_once()


def test_run_training_with_dataclass_config() -> None:
    from chalkcompute._gen.chalk.server.v1 import training_runs_pb as tr_pb

    @dataclass
    class MyConfig:
        lr: float = 0.01

    @training(data="ds")
    def train(df: Any, config: Any) -> str:
        return "ok"

    mock_client = MagicMock()
    mock_run = tr_pb.TrainingRun(id="run-dc")
    mock_resp = tr_pb.CreateTrainingRunResponse(training_run=mock_run)
    mock_client.call.return_value = mock_resp

    with (
        patch.object(TrainingFunction, "_build_deploy_image", return_value=_mock_deploy_image()),
        patch("chalkcompute._training.build_image", return_value="img:latest"),
    ):
        handle = train.run_training(config=MyConfig(), client=mock_client)
    assert handle.run_id == "run-dc"


def test_run_training_builds_image_when_provided() -> None:
    from chalkcompute._gen.chalk.server.v1 import training_runs_pb as tr_pb

    mock_image = MagicMock()
    with patch.object(ContainerSpec, "validate_fields"):
        tf = TrainingFunction(
            lambda df, config: "ok",
            data="ds",
            image=mock_image,
        )

    mock_client = MagicMock()
    mock_run = tr_pb.TrainingRun(id="run-img")
    mock_resp = tr_pb.CreateTrainingRunResponse(training_run=mock_run)
    mock_client.call.return_value = mock_resp

    with (
        patch.object(TrainingFunction, "_build_deploy_image", return_value=_mock_deploy_image()),
        patch("chalkcompute._training.build_image", return_value="img:latest") as bi,
    ):
        handle = tf.run_training(config=None, client=mock_client)
        bi.assert_called_once()
    assert handle.run_id == "run-img"
