from __future__ import annotations

from typing import Any

import pytest

import chalkcompute as cc
from chalkcompute import _workflow
from chalkcompute._workflow_worker import WorkflowWorker


class _RecordingClient:
    def __init__(self) -> None:
        self.environments: list[str | None] = []
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

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


def test_trigger_workflow_delegates_to_chalk_client(client: _RecordingClient) -> None:
    assert cc.trigger_workflow("my_workflow", wait=True) == "triggered"
    assert client.calls == [
        ("trigger_workflow", ("my_workflow", None), {"workflow_id": None, "wait": True})
    ]
    assert client.environments == [None]


def test_trigger_workflow_targets_a_worker_by_name(client: _RecordingClient) -> None:
    cc.trigger_workflow("score", {"segment": "us"}, worker="wf-nightly")
    assert client.calls == [
        (
            "trigger_workflow",
            ("score", {"segment": "us"}),
            {
                "workflow_id": None,
                "wait": False,
                "task_queue": "chalkcompute-workflows.wf-nightly",
            },
        )
    ]


def test_trigger_workflow_targets_a_worker_object(client: _RecordingClient) -> None:
    worker = WorkflowWorker(
        scaling_group=None,  # type: ignore[arg-type]
        task_queue="chalkcompute-workflows.wf-custom",
    )
    cc.trigger_workflow("score", worker=worker, workflow_id="run-1", wait=True)
    assert client.calls == [
        (
            "trigger_workflow",
            ("score", None),
            {
                "workflow_id": "run-1",
                "wait": True,
                "task_queue": "chalkcompute-workflows.wf-custom",
            },
        )
    ]


def test_trigger_workflow_name_matches_the_deployed_queue(
    client: _RecordingClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from chalkcompute import _workflow_worker

    class _Group:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def deploy(self) -> _Group:
            return self

    monkeypatch.setattr(_workflow_worker, "ScalingGroup", _Group)
    source = tmp_path / "nightly.py"
    source.write_text("from chalk.workflows import workflow\n")
    worker = cc.deploy_workflow_worker(str(source), image=cc.Image.debian_slim())

    cc.trigger_workflow("score", worker=worker.scaling_group.kwargs["name"])
    assert client.calls[0][2]["task_queue"] == worker.task_queue
    assert worker.scaling_group.kwargs["workflow_task_queue"] == worker.task_queue


@pytest.mark.parametrize(
    ("worker", "error"),
    [
        ("", ValueError),
        ("chalkcompute-workflows.wf-nightly", ValueError),
        (3, TypeError),
    ],
)
def test_trigger_workflow_rejects_invalid_workers(
    client: _RecordingClient, worker: Any, error: type[Exception]
) -> None:
    with pytest.raises(error):
        cc.trigger_workflow("score", worker=worker)
    assert client.calls == []
