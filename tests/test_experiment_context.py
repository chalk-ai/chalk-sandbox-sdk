from __future__ import annotations

import io
import json
import os
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import chalkcompute
from chalkcompute import _experiment_context as context_module
from chalkcompute._experiment_context import (
    ExperimentContextError,
    _publish_experiment_context,
    experiment_context,
)


def _experiment(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    return {
        "experiment_id": "exp-1",
        "evaluation_id": "eval-1",
        "base_model": "Qwen/Qwen3-4B-Instruct-2507",
        "baseline_run_id": "run-base",
        "adapter_out": "adapter-exp-1-1",
        "adapter_dir": str(tmp_path / "adapters"),
        "policy_server_url": "http://policy.internal:8000/",
        "policy_adapter_dir": "/chalk/adapters",
        **overrides,
    }


@pytest.fixture(autouse=True)
def _no_ambient_experiment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("POLICY_SERVER_API_KEY", raising=False)
    # Publishing writes the process environment directly, so clear it on both sides.
    os.environ.pop("CHALK_EXPERIMENT_CONTEXT", None)
    yield
    os.environ.pop("CHALK_EXPERIMENT_CONTEXT", None)


class _FakeModel:
    def save_pretrained(self, path: str) -> None:
        Path(path).mkdir(parents=True)
        (Path(path) / "adapter_model.safetensors").write_bytes(b"weights")
        (Path(path) / "adapter_config.json").write_text("{}")


class _Response:
    status = 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _http_error(code: int, detail: str = "not yet") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://policy.internal:8000/v1/load_lora_adapter",
        code,
        "error",
        None,  # type: ignore[arg-type]
        io.BytesIO(detail.encode()),
    )


def test_is_exported_from_the_package() -> None:
    assert chalkcompute.experiment_context is experiment_context


def test_outside_an_experiment_it_says_so() -> None:
    with pytest.raises(ExperimentContextError, match="not part of an experiment"):
        experiment_context()


def test_reads_the_experiment_the_entrypoint_published(tmp_path: Path) -> None:
    _publish_experiment_context({"lr": 2e-5, "experiment": _experiment(tmp_path)})

    ctx = experiment_context()

    assert (ctx.experiment_id, ctx.evaluation_id) == ("exp-1", "eval-1")
    assert ctx.base_model == "Qwen/Qwen3-4B-Instruct-2507"
    assert ctx.baseline_run_id == "run-base"
    assert ctx.adapter_path == tmp_path / "adapters" / "adapter-exp-1-1"


def test_a_run_of_its_own_publishes_nothing() -> None:
    _publish_experiment_context({"lr": 2e-5})
    _publish_experiment_context(None)

    with pytest.raises(ExperimentContextError):
        experiment_context()


def test_an_experiment_missing_a_field_names_it(tmp_path: Path) -> None:
    experiment = _experiment(tmp_path)
    del experiment["policy_server_url"]
    _publish_experiment_context({"experiment": experiment})

    with pytest.raises(ExperimentContextError, match="policy_server_url"):
        experiment_context()


def test_finish_saves_checkpoints_and_serves_the_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_experiment_context({"experiment": _experiment(tmp_path)})
    steps: list[str] = []
    checkpoints: list[tuple[list[str], dict[str, Any]]] = []
    requests: list[Any] = []

    monkeypatch.setattr(
        context_module, "_commit_mount", lambda path: steps.append(f"commit {path}")
    )

    def record(_model: Any, files: list[str], metadata: dict[str, Any]) -> None:
        steps.append("checkpoint")
        checkpoints.append((files, metadata))

    def urlopen(request: Any, timeout: float) -> _Response:
        steps.append("load")
        requests.append(request)
        return _Response()

    monkeypatch.setattr(context_module, "_record_checkpoint", record)
    monkeypatch.setattr(context_module.urllib.request, "urlopen", urlopen)

    # An adapter a failed attempt left behind is replaced, not merged into.
    stale = tmp_path / "adapters" / "adapter-exp-1-1"
    stale.mkdir(parents=True)
    (stale / "stale.bin").write_bytes(b"old")

    path = experiment_context().finish(_FakeModel(), metadata={"epochs": 3})

    assert path == stale
    assert sorted(file.name for file in path.iterdir()) == [
        "adapter_config.json",
        "adapter_model.safetensors",
    ]
    # Saved and committed before anything reads it back.
    assert steps == [f"commit {tmp_path / 'adapters'}", "checkpoint", "load"]
    [(files, metadata)] = checkpoints
    assert [Path(file).name for file in files] == [
        "adapter_config.json",
        "adapter_model.safetensors",
    ]
    assert metadata == {
        "epochs": 3,
        "experiment_id": "exp-1",
        "adapter_name": "adapter-exp-1-1",
        "base_model": "Qwen/Qwen3-4B-Instruct-2507",
    }
    [request] = requests
    assert request.full_url == "http://policy.internal:8000/v1/load_lora_adapter"
    # The server loads from its own mount of the volume, not the training run's.
    assert json.loads(request.data) == {
        "lora_name": "adapter-exp-1-1",
        "lora_path": "/chalk/adapters/adapter-exp-1-1",
        "load_inplace": True,
    }


def test_load_retries_until_the_server_sees_the_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes: list[Any] = [
        _http_error(404),
        urllib.error.URLError("connection refused"),
        _Response(),
    ]
    sleeps: list[float] = []

    def urlopen(_request: Any, timeout: float) -> Any:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(context_module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(context_module.time, "sleep", sleeps.append)

    context_module._load_adapter("http://policy", "adapter", "/a/adapter", 900.0)

    assert outcomes == []
    assert sleeps == [5.0, 10.0]


def test_load_gives_up_at_the_timeout_with_the_last_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def urlopen(_request: Any, timeout: float) -> Any:
        raise _http_error(404, "adapter path not found")

    monkeypatch.setattr(context_module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(context_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(ExperimentContextError, match="adapter path not found"):
        context_module._load_adapter("http://policy", "adapter", "/a/adapter", 0.0)


def test_load_does_not_retry_a_refused_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def urlopen(request: Any, timeout: float) -> Any:
        calls.append(request)
        raise _http_error(401, "bad key")

    monkeypatch.setenv("POLICY_SERVER_API_KEY", "secret")
    monkeypatch.setattr(context_module.urllib.request, "urlopen", urlopen)

    with pytest.raises(ExperimentContextError, match="refused the key"):
        context_module._load_adapter("http://policy", "adapter", "/a/adapter", 900.0)

    [request] = calls
    assert request.get_header("Authorization") == "Bearer secret"
