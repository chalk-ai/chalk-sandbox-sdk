from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from chalkcompute._gen.chalk.server.v1 import (
    training_runs_pb as tr_pb,
)
from chalkcompute.training.entrypoint import (
    _deserialize_config,
    _discover_function,
    _load_dataframe,
    _update_status,
    main,
)


@dataclass
class _TestCfg:
    lr: float = 0.0
    epochs: float = 0.0


@dataclass
class _StrictCfg:
    required_field: str


# --- _update_status ---


def test_update_status_working() -> None:
    mock_client = MagicMock()
    _update_status(mock_client, "run-1", tr_pb.TrainingRunStatus.WORKING)

    mock_client.call.assert_called_once()
    req = mock_client.call.call_args[0][1]
    assert req.training_run_id == "run-1"
    assert req.update.status == tr_pb.TrainingRunStatus.WORKING
    assert list(req.update_mask.paths) == ["status"]


def test_update_status_failed_with_error() -> None:
    mock_client = MagicMock()
    _update_status(
        mock_client,
        "run-2",
        tr_pb.TrainingRunStatus.FAILED,
        error_message="boom",
    )

    req = mock_client.call.call_args[0][1]
    assert req.update.status == tr_pb.TrainingRunStatus.FAILED
    assert req.update.error_message == "boom"
    assert "status" in req.update_mask.paths
    assert "error_message" in req.update_mask.paths


# --- _discover_function ---


def test_discover_function() -> None:
    func = _discover_function("chalkcompute._training.TrainingFunction")
    from chalkcompute._training import TrainingFunction

    assert func is TrainingFunction


def test_discover_function_bad_format() -> None:
    with pytest.raises(RuntimeError, match="<module>.<function>"):
        _discover_function("no_dot_here")


def test_discover_function_not_found() -> None:
    with pytest.raises(RuntimeError, match="not found"):
        _discover_function("chalkcompute._training.does_not_exist")


# --- _deserialize_config ---


def test_deserialize_config_none() -> None:
    assert _deserialize_config(None, lambda df, config: None) is None


def test_deserialize_config_dict() -> None:
    from protobuf.wkt import Struct

    s = Struct.from_python({"lr": 0.01, "epochs": 10})
    result = _deserialize_config(s, lambda df, config: None)
    assert isinstance(result, dict)
    assert result["lr"] == pytest.approx(0.01)
    assert result["epochs"] == pytest.approx(10.0)


def test_deserialize_config_dataclass() -> None:
    from protobuf.wkt import Struct

    def my_train(df: Any, config: _TestCfg) -> None:
        pass

    s = Struct.from_python({"lr": 0.01, "epochs": 10})
    result = _deserialize_config(s, my_train)
    assert isinstance(result, _TestCfg)
    assert result.lr == pytest.approx(0.01)


def test_deserialize_config_dataclass_fallback() -> None:
    from protobuf.wkt import Struct

    def my_train(df: Any, config: _StrictCfg) -> None:
        pass

    s = Struct.from_python({"unexpected": "data"})
    result = _deserialize_config(s, my_train)
    assert isinstance(result, dict)


# --- _load_dataframe ---


def test_load_dataframe_no_files(tmp_path: Path) -> None:
    assert _load_dataframe(str(tmp_path)) is None


def test_load_dataframe_parquet(tmp_path: Path) -> None:
    try:
        import polars as pl
    except ImportError:
        pytest.skip("polars not installed")

    df = pl.DataFrame({"a": [1, 2], "b": [3, 4]})
    df.write_parquet(tmp_path / "part-0.parquet")

    result = _load_dataframe(str(tmp_path))
    assert result is not None
    collected = result.collect()
    assert len(collected) == 2


# --- main ---


def test_main_missing_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHALK_TRAINING_RUN_ID", raising=False)
    with pytest.raises(SystemExit):
        main()


def test_main_missing_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHALK_TRAINING_RUN_ID", "run-abc")
    monkeypatch.delenv("CHALK_TRAINING_MODULE", raising=False)
    with pytest.raises(SystemExit):
        main()


def test_main_happy_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from protobuf.wkt import Struct

    monkeypatch.setenv("CHALK_TRAINING_RUN_ID", "abcdef12-0000-0000-0000-000000000000")
    monkeypatch.setenv(
        "CHALK_TRAINING_MODULE", "chalkcompute._training._NoOpTrainingFunction"
    )
    monkeypatch.setenv("CHALK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHALK_CHECKPOINT_DIR", str(tmp_path / "ckpt"))
    monkeypatch.setenv("CHALK_DATASET_REVISION_ID", "")

    mock_run = tr_pb.TrainingRun(
        id="abcdef12-0000-0000-0000-000000000000",
        config=Struct.from_python({"lr": 0.01}),
    )

    mock_client = MagicMock()
    mock_client.call.return_value = tr_pb.GetTrainingRunResponse(
        training_run=mock_run,
    )

    fake_func = MagicMock(return_value="model")

    with (
        patch(
            "chalkcompute.training.entrypoint.ConnectClient", return_value=mock_client
        ),
        patch(
            "chalkcompute.training.entrypoint._discover_function",
            return_value=fake_func,
        ),
    ):
        main()

    status_calls = [
        c
        for c in mock_client.call.call_args_list
        if len(c[0]) >= 2 and isinstance(c[0][1], tr_pb.UpdateTrainingRunRequest)
    ]
    assert len(status_calls) == 2
    assert status_calls[0][0][1].update.status == tr_pb.TrainingRunStatus.WORKING
    assert status_calls[1][0][1].update.status == tr_pb.TrainingRunStatus.COMPLETED

    fake_func.assert_called_once()


def test_main_function_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from protobuf.wkt import Struct

    monkeypatch.setenv("CHALK_TRAINING_RUN_ID", "abcdef12-0000-0000-0000-000000000000")
    monkeypatch.setenv("CHALK_TRAINING_MODULE", "my_mod.train")
    monkeypatch.setenv("CHALK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHALK_CHECKPOINT_DIR", str(tmp_path / "ckpt"))
    monkeypatch.setenv("CHALK_DATASET_REVISION_ID", "")

    mock_run = tr_pb.TrainingRun(
        id="abcdef12-0000-0000-0000-000000000000",
        config=Struct.from_python({}),
    )
    mock_client = MagicMock()
    mock_client.call.return_value = tr_pb.GetTrainingRunResponse(
        training_run=mock_run,
    )

    def boom(*args: Any, **kwargs: Any) -> None:
        raise ValueError("training crashed")

    with (
        patch(
            "chalkcompute.training.entrypoint.ConnectClient", return_value=mock_client
        ),
        patch("chalkcompute.training.entrypoint._discover_function", return_value=boom),
        pytest.raises(SystemExit),
    ):
        main()

    update_calls = [
        c
        for c in mock_client.call.call_args_list
        if len(c[0]) >= 2 and isinstance(c[0][1], tr_pb.UpdateTrainingRunRequest)
    ]
    statuses = [c[0][1].update.status for c in update_calls]
    assert tr_pb.TrainingRunStatus.WORKING in statuses
    assert tr_pb.TrainingRunStatus.FAILED in statuses

    failed_call = [
        c
        for c in update_calls
        if c[0][1].update.status == tr_pb.TrainingRunStatus.FAILED
    ][0]
    assert "training crashed" in failed_call[0][1].update.error_message
