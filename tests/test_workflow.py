from __future__ import annotations

from typing import Any

import pytest

import chalkcompute as cc
from chalkcompute import _workflow


class _RecordingClient:
    def __init__(self) -> None:
        self.environments: list[str | None] = []
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

    def fake_client(*, environment: str | None = None) -> _RecordingClient:
        recording.environments.append(environment)
        return recording

    monkeypatch.setattr(_workflow, "chalk_client_from_env", fake_client)
    return recording


def test_run_workflow_delegates_to_chalk_client(client: _RecordingClient) -> None:
    wf: Any = object()
    result = cc.run_workflow(wf, {"n": 3}, workflow_id="wf-1", environment="env-1")
    assert result == "ran"
    assert client.calls == [("run_workflow", (wf, {"n": 3}), {"workflow_id": "wf-1"})]
    assert client.environments == ["env-1"]


def test_trigger_workflow_delegates_to_chalk_client(client: _RecordingClient) -> None:
    assert cc.trigger_workflow("my_workflow", wait=True) == "triggered"
    assert client.calls == [
        ("trigger_workflow", ("my_workflow", None), {"workflow_id": None, "wait": True})
    ]
    assert client.environments == [None]
