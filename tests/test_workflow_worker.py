from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

import chalkcompute._workflow_worker as workflow_worker_mod
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._scaling_group import (
    CronScalingTrigger,
    CronScalingWindow,
    ScalingGroup,
)
from chalkcompute._image import Image
from chalkcompute._workflow_worker import (
    default_worker_name,
    deploy_workflow_worker,
    image_key,
    script_dependencies,
    task_worker_name,
    worker_requirements,
)
from tests.test_deployment_lifecycle import _ScalingLifecycleClient

_SCRIPT = textwrap.dedent(
    """\
    # /// script
    # requires-python = ">=3.12"
    # dependencies = [
    #   "chalkpy>=2.160",
    #   "chalkcompute",
    #   "numpy",
    # ]
    # ///
    import chalkcompute as cc
    """
)


def test_scaling_group_sends_task_queue_trigger() -> None:
    group = ScalingGroup(
        image="ubuntu:latest",
        name="wf-demo",
        port=None,
        min_replicas=0,
        max_replicas=2,
        workflow_task_queue="chalkcompute-workflows.wf-demo",
    )
    client = _ScalingLifecycleClient()
    group._client = client  # type: ignore[assignment]

    group.deploy()

    request = client.calls[0][1]
    assert (
        request.spec.scaling_spec.task_queue_trigger
        == sg_pb.TaskQueueScalingTrigger(task_queue="chalkcompute-workflows.wf-demo")
    )
    assert request.spec.scaling_spec.min_replicas == 0


def test_scaling_group_omits_trigger_by_default() -> None:
    group = ScalingGroup(image="ubuntu:latest", name="plain")
    client = _ScalingLifecycleClient()
    group._client = client  # type: ignore[assignment]

    group.deploy()

    assert client.calls[0][1].spec.scaling_spec.task_queue_trigger is None


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"workflow_task_queue": ""}, "non-empty"),
        ({"workflow_task_queue": "q'x"}, "quotes"),
        ({"workflow_task_queue": "q", "min_replicas": 1}, "min_replicas=0"),
        (
            {
                "workflow_task_queue": "q",
                "cron_scaling_trigger": CronScalingTrigger(
                    timezone="UTC",
                    windows=[
                        CronScalingWindow(
                            start="0 8 * * *", end="0 9 * * *", desired_replicas=1
                        )
                    ],
                ),
            },
            "cannot be combined",
        ),
    ],
)
def test_scaling_group_rejects_invalid_workflow_task_queue(
    kwargs: dict[str, Any], message: str
) -> None:
    kwargs = {"min_replicas": 0, **kwargs}
    with pytest.raises(ValueError, match=message):
        ScalingGroup(image="ubuntu:latest", name="bad", **kwargs)


def test_script_dependencies_reads_the_pep_723_block() -> None:
    assert script_dependencies(_SCRIPT) == ["chalkpy>=2.160", "chalkcompute", "numpy"]
    assert script_dependencies("import os\n") is None


def test_worker_requirements_adds_the_workflows_extra_and_keeps_pins() -> None:
    assert worker_requirements(_SCRIPT) == [
        "chalkpy[workflows]>=2.160",
        "chalkcompute",
        "numpy",
    ]

    no_chalkpy = '# /// script\n# dependencies = ["requests"]\n# ///\n'
    assert worker_requirements(no_chalkpy) == ["chalkpy[workflows]", "requests"]

    other_extra = (
        '# /// script\n# dependencies = ["chalkpy[runtime]==2.161.0"]\n# ///\n'
    )
    assert worker_requirements(other_extra) == ["chalkpy[workflows]==2.161.0"]


def test_worker_requirements_without_metadata_pins_the_local_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workflow_worker_mod,
        "_installed_version",
        lambda dist: {"chalkpy": "2.161.0", "chalkcompute": "2.13.0"}[dist],
    )
    assert worker_requirements("from chalk.workflows import workflow\n") == [
        "chalkpy[workflows]==2.161.0"
    ]
    assert worker_requirements("import chalkcompute as cc\n") == [
        "chalkpy[workflows]==2.161.0",
        "chalkcompute==2.13.0",
    ]


def test_default_worker_name_is_a_dns_label() -> None:
    assert default_worker_name("/tmp/Nightly_Scoring.py") == "wf-nightly-scoring"


class _RecordingScalingGroup:
    instances: list[_RecordingScalingGroup] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        _RecordingScalingGroup.instances.append(self)

    def deploy(self) -> _RecordingScalingGroup:
        return self


def test_deploy_workflow_worker_builds_a_task_queue_triggered_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "nightly_scoring.py"
    source.write_text(_SCRIPT)
    _RecordingScalingGroup.instances.clear()
    monkeypatch.setattr(workflow_worker_mod, "ScalingGroup", _RecordingScalingGroup)

    worker = deploy_workflow_worker(str(source), max_replicas=3)

    assert worker.task_queue == "chalkcompute-workflows.wf-nightly-scoring"
    (group,) = _RecordingScalingGroup.instances
    kwargs = group.kwargs
    assert kwargs["name"] == "wf-nightly-scoring"
    assert kwargs["workflow_task_queue"] == worker.task_queue
    assert kwargs["min_replicas"] == 0
    assert kwargs["max_replicas"] == 3
    assert kwargs["port"] is None
    assert kwargs["entrypoint"] == [
        "python",
        "-m",
        "chalk.workflows.worker",
        "/app/nightly_scoring.py",
    ]
    image = kwargs["image"]
    assert (
        image._base_image
        == f"python:{sys.version_info.major}.{sys.version_info.minor}-slim-trixie"
    )
    assert {(f.src, f.dest) for f in image.lazy_local_files} == {
        (str(source), "/app/nightly_scoring.py")
    }


_IMAGE_TASKS_SCRIPT = _SCRIPT + textwrap.dedent(
    """\
    from chalk.workflows import task, workflow

    heavy = cc.Image.debian_slim("3.12").pip_install(["torch"])

    @task
    def plain(x: int) -> int:
        return x

    @task(image=heavy)
    def embed(x: int) -> int:
        return x

    @task(image=cc.Image.debian_slim("3.12").pip_install(["torch"]))
    def rerank(x: int) -> int:
        return x

    @task(image="ghcr.io/acme/tools:1")
    def tool(x: int) -> int:
        return x

    @workflow
    async def pipeline(x: int) -> int:
        return await tool(await rerank(await embed(await plain(x))))
    """
)


def test_deploy_workflow_worker_gives_each_task_image_its_own_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "image_tasks.py"
    source.write_text(_IMAGE_TASKS_SCRIPT)
    _RecordingScalingGroup.instances.clear()
    monkeypatch.setattr(workflow_worker_mod, "ScalingGroup", _RecordingScalingGroup)

    worker = deploy_workflow_worker(str(source), env={"LOG_LEVEL": "debug"})

    heavy_key = image_key(Image.debian_slim("3.12").pip_install(["torch"]))
    tool_key = image_key(Image.base("ghcr.io/acme/tools:1"))
    heavy_group, tool_group, main_group = _RecordingScalingGroup.instances
    # Equal images share a group, whether or not they are the same object.
    assert heavy_group.kwargs["name"] == f"wf-image-tasks-{heavy_key}"
    assert heavy_group.kwargs["entrypoint"][-2:] == ["--tasks", "embed,rerank"]
    assert tool_group.kwargs["name"] == f"wf-image-tasks-{tool_key}"
    assert tool_group.kwargs["entrypoint"][-2:] == ["--tasks", "tool"]
    assert worker.task_workers == {
        "embed": heavy_group,
        "rerank": heavy_group,
        "tool": tool_group,
    }

    for group in (heavy_group, tool_group):
        kwargs = group.kwargs
        assert (
            kwargs["workflow_task_queue"] == f"chalkcompute-workflows.{kwargs['name']}"
        )
        assert kwargs["min_replicas"] == 0
        assert kwargs["env"] == {"LOG_LEVEL": "debug"}
        assert kwargs["entrypoint"][:4] == [
            "python",
            "-m",
            "chalk.workflows.worker",
            "/app/image_tasks.py",
        ]
        # The task worker imports the file, so it gets the script's packages and the file itself.
        assert {(f.src, f.dest) for f in kwargs["image"].lazy_local_files} == {
            (str(source), "/app/image_tasks.py")
        }
    assert tool_group.kwargs["image"]._base_image == "ghcr.io/acme/tools:1"
    assert heavy_group.kwargs["image"]._base_image == "python:3.12-slim-trixie"

    assert main_group.kwargs["name"] == "wf-image-tasks"
    assert main_group.kwargs["entrypoint"] == [
        "python",
        "-m",
        "chalk.workflows.worker",
        "/app/image_tasks.py",
    ]
    main_env = main_group.kwargs["env"]
    assert main_env["LOG_LEVEL"] == "debug"
    assert json.loads(main_env["CHALK_WORKFLOW_TASK_QUEUES"]) == {
        "embed": f"chalkcompute-workflows.wf-image-tasks-{heavy_key}",
        "rerank": f"chalkcompute-workflows.wf-image-tasks-{heavy_key}",
        "tool": f"chalkcompute-workflows.wf-image-tasks-{tool_key}",
    }


def test_deploy_workflow_worker_without_task_images_sets_no_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "no_images.py"
    source.write_text(_SCRIPT)
    _RecordingScalingGroup.instances.clear()
    monkeypatch.setattr(workflow_worker_mod, "ScalingGroup", _RecordingScalingGroup)

    worker = deploy_workflow_worker(str(source))

    (group,) = _RecordingScalingGroup.instances
    assert group.kwargs["env"] is None
    assert worker.task_workers == {}


def test_image_key_tracks_the_whole_image() -> None:
    base = Image.debian_slim("3.12")
    assert image_key(base.pip_install(["numpy"])) == image_key(
        Image.debian_slim("3.12").pip_install(["numpy"])
    )
    assert image_key(base) != image_key(base.pip_install(["numpy"]))
    assert image_key(base) != image_key(base.add_local_file(__file__, "/app/x.py"))


def test_task_worker_name_keeps_the_image_key() -> None:
    name = task_worker_name("wf-" + "a" * 37, "0123abcd")
    assert len(name) <= 40
    assert name.endswith("-0123abcd")
    assert task_worker_name("wf-demo", "0123abcd") == "wf-demo-0123abcd"


class _RevisionHistoryClient(_ScalingLifecycleClient):
    """Reports one revision whose uploaded-source volume carries the API's unset-fork value."""

    def call(self, method: Any, request: Any, **kwargs: Any) -> Any:
        if method.name == "list_scaling_group_revisions":
            from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb

            return sg_pb.ListScalingGroupRevisionsResponse(
                revisions=[
                    sg_pb.ScalingGroupRevisionResponse(
                        id="revision-1",
                        scaling_group_id="group-1",
                        spec=sg_pb.ScalingGroupSpec(
                            container_spec=container_pb.ChalkContainerSpec(
                                volumes=[
                                    container_pb.VolumeMount(
                                        name="chalkcompute-sg-files-abc",
                                        mount_path="/app",
                                        ref_name="",
                                    )
                                ]
                            )
                        ),
                    )
                ]
            )
        return super().call(method, request, **kwargs)


def test_owned_volumes_from_revisions_treats_an_empty_fork_as_unset() -> None:
    group = ScalingGroup(image="ubuntu:latest", name="api")
    group._client = _RevisionHistoryClient()  # type: ignore[assignment]
    group._scaling_group_id = "group-1"

    volumes = group._owned_volumes_from_revisions()

    assert volumes["chalkcompute-sg-files-abc"].ref_name is None
    assert volumes["chalkcompute-sg-files-abc"].mount_path == "/app"
