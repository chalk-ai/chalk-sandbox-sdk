from __future__ import annotations

import os
from typing import Any

import pytest

from chalkcompute._training import (
    TrainingError,
    TrainingFunction,
    _NoOpTrainingFunction,
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


def test_run_training_raises_not_implemented() -> None:
    @training(data="ds")
    def train(df: Any, config: Any) -> str:
        return "ok"

    with pytest.raises(NotImplementedError, match="CHA-10115"):
        train.run_training(config="c")


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
