from __future__ import annotations

from typing import Any

import pytest

import chalkcompute as cc
from chalkcompute import _workflow


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def run_workflow(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("run_workflow", args, kwargs))
        return "ran"

    def trigger_workflow(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("trigger_workflow", args, kwargs))
        return "triggered"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _RecordingClient:
    recording = _RecordingClient()
    environments: list[str | None] = []

    def fake_chalk_client_from_env(*, environment: str | None = None) -> _RecordingClient:
        environments.append(environment)
        return recording

    monkeypatch.setattr(_workflow, "chalk_client_from_env", fake_chalk_client_from_env)
    recording.environments = environments  # type: ignore[attr-defined]
    return recording


def test_run_workflow_delegates_to_chalk_client(client: _RecordingClient) -> None:
    wf = object()
    assert cc.run_workflow(wf, {"n": 3}, workflow_id="wf-1", environment="env-1") == "ran"  # type: ignore[arg-type]
    assert client.calls == [("run_workflow", (wf, {"n": 3}), {"workflow_id": "wf-1"})]
    assert client.environments == ["env-1"]  # type: ignore[attr-defined]


def test_trigger_workflow_delegates_to_chalk_client(client: _RecordingClient) -> None:
    assert cc.trigger_workflow("my_workflow", wait=True) == "triggered"
    assert client.calls == [
        ("trigger_workflow", ("my_workflow", None), {"workflow_id": None, "wait": True})
    ]
    assert client.environments == [None]  # type: ignore[attr-defined]
