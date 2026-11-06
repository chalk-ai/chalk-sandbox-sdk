from __future__ import annotations

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
from chalkcompute._workflow_worker import (
    default_worker_name,
    deploy_workflow_worker,
    script_dependencies,
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
    assert kwargs["chalk_identity"] is False


def test_deploy_workflow_worker_passes_chalk_identity_to_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "nightly_scoring.py"
    source.write_text(_SCRIPT)
    _RecordingScalingGroup.instances.clear()
    monkeypatch.setattr(workflow_worker_mod, "ScalingGroup", _RecordingScalingGroup)

    deploy_workflow_worker(str(source), chalk_identity=True)

    (group,) = _RecordingScalingGroup.instances
    assert group.kwargs["chalk_identity"] is True


def test_task_queue_group_sends_chalk_identity() -> None:
    group = ScalingGroup(
        image="ubuntu:latest",
        name="wf-demo",
        port=None,
        min_replicas=0,
        workflow_task_queue="chalkcompute-workflows.wf-demo",
        chalk_identity=True,
    )
    client = _ScalingLifecycleClient()
    group._client = client  # type: ignore[assignment]

    group.deploy()

    assert client.calls[0][1].spec.container_spec.chalk_workload_identity is not None


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


def _chalkpy_has_task_workers() -> bool:
    try:
        import chalk.workflows.worker  # noqa: F401  # ty: ignore[unresolved-import]
    except ImportError:
        return False
    return workflow_worker_mod._chalkpy_has_task_resources()


requires_task_resources = pytest.mark.skipif(
    not _chalkpy_has_task_workers(),
    reason="needs a chalkpy with chalk.workflows.TaskResources and chalk.workflows.worker",
)


_TASKS_SCRIPT = """\
# /// script
# dependencies = ["chalkpy>=2.160", "chalkcompute", "pandas"]
# ///
import chalkcompute as cc
from chalk.workflows import TaskResources, task, workflow

numpy_image = cc.Image.debian_slim("3.12").pip_install(["numpy"])
heavy = TaskResources(cpu=2, memory="4Gi", max_replicas=3, max_concurrent_tasks=1)

@task(image=numpy_image)
def mean_SUFFIX(xs: list[float]) -> float:
    return sum(xs) / len(xs)

@task(image=cc.Image.debian_slim("3.12").pip_install(["numpy"]))
def total_SUFFIX(xs: list[float]) -> float:
    return sum(xs)

@task(image=numpy_image, resources=heavy)
def big_mean_SUFFIX(xs: list[float]) -> float:
    return sum(xs) / len(xs)

@task(resources=heavy)
def HEAVY_A_SUFFIX(month: str) -> str:
    return month

@task(resources=heavy)
def heavy_b_SUFFIX(month: str) -> str:
    return month

@task
def plain_SUFFIX(x: int) -> int:
    return x

@workflow
async def wf_SUFFIX() -> None:
    pass
"""


def _write_tasks_script(
    tmp_path: Path, body: str = _TASKS_SCRIPT, stem: str = "ranker"
) -> tuple[Path, str]:
    import uuid

    suffix = uuid.uuid4().hex[:8]
    path = tmp_path / suffix / f"{stem}.py"
    path.parent.mkdir()
    path.write_text(body.replace("SUFFIX", suffix).replace("HEAVY_A", "heavy_a"))
    return path, suffix


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[_RecordingScalingGroup]:
    _RecordingScalingGroup.instances = []
    monkeypatch.setattr(workflow_worker_mod, "ScalingGroup", _RecordingScalingGroup)
    monkeypatch.setattr(
        workflow_worker_mod, "_delete_stale_task_workers", lambda *a, **k: []
    )
    return _RecordingScalingGroup.instances


def _groups_by_tasks(
    groups: list[_RecordingScalingGroup],
) -> dict[tuple[str, ...], dict[str, Any]]:
    by_tasks: dict[tuple[str, ...], dict[str, Any]] = {}
    for g in groups:
        entrypoint = g.kwargs["entrypoint"]
        tasks = (
            tuple(entrypoint[entrypoint.index("--tasks") + 1].split(","))
            if "--tasks" in entrypoint
            else ()
        )
        by_tasks[tasks] = g.kwargs
    return by_tasks


@requires_task_resources
def test_tasks_get_one_group_per_image_and_resources(
    tmp_path: Path, recorded: list[_RecordingScalingGroup]
) -> None:
    import json

    source, s = _write_tasks_script(tmp_path)

    worker = deploy_workflow_worker(
        str(source), cpu="1", memory="2Gi", max_concurrent_tasks=4
    )

    groups = _groups_by_tasks(recorded)
    images = groups[(f"mean_{s}", f"total_{s}")]
    big = groups[(f"big_mean_{s}",)]
    heavy = groups[(f"heavy_a_{s}", f"heavy_b_{s}")]
    main = groups[()]
    assert len(recorded) == 4
    # Task groups deploy before the group that routes to them.
    assert recorded[-1].kwargs is main

    # Equal images from separate objects share a group with the deploy's resources.
    assert (images["cpu"], images["memory"], images["max_replicas"]) == ("1", "2Gi", 1)
    assert images["entrypoint"][-2:] == ["--max-concurrent-tasks", "4"]
    assert images["target_cpu_utilization_percentage"] is None
    assert "numpy" in str(images["image"]._to_build_proto())
    assert "pandas" not in str(images["image"]._to_build_proto())

    # A task's resources override the deploy's, and its own image still splits it off.
    for g in (big, heavy):
        assert (g["cpu"], g["memory"], g["max_replicas"]) == ("2", "4Gi", 3)
        assert g["entrypoint"][-2:] == ["--max-concurrent-tasks", "1"]
        assert g["target_cpu_utilization_percentage"] == 60
        assert g["min_replicas"] == 0
    assert big["name"] != heavy["name"]

    # A task with only resources runs on the worker's own image.
    assert heavy["image"] is main["image"]
    assert {(f.dest) for f in big["image"].lazy_local_files} == {"/app/ranker.py"}

    for g in (images, big, heavy):
        assert (
            g["name"].startswith("wf-ranker-")
            and len(g["name"]) == len("wf-ranker-") + 8
        )
        assert g["workflow_task_queue"] == f"chalkcompute-workflows.{g['name']}"
        assert g["tags"] == {"chalk.workflow-worker": "wf-ranker"}
        assert g["entrypoint"][:4] == [
            "python",
            "-m",
            "chalk.workflows.worker",
            "/app/ranker.py",
        ]

    routes = json.loads(main["env"]["CHALK_WORKFLOW_TASK_QUEUES"])
    assert routes == {
        f"mean_{s}": images["workflow_task_queue"],
        f"total_{s}": images["workflow_task_queue"],
        f"big_mean_{s}": big["workflow_task_queue"],
        f"heavy_a_{s}": heavy["workflow_task_queue"],
        f"heavy_b_{s}": heavy["workflow_task_queue"],
    }
    assert main["entrypoint"][4:] == ["--max-concurrent-tasks", "4"]
    assert main["tags"] is None
    assert set(worker.task_workers) == set(routes)


@requires_task_resources
def test_task_group_names_survive_source_edits_but_not_resource_changes(
    tmp_path: Path,
    recorded: list[_RecordingScalingGroup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Edits within one second that keep the file's size would otherwise reuse its bytecode.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    source, s = _write_tasks_script(tmp_path)
    deploy_workflow_worker(str(source))
    first = {g.kwargs["name"] for g in recorded}

    # Renaming a task and editing the file keep every group; the renamed task moves with its group.
    recorded.clear()
    source.write_text(
        source.read_text().replace(f"heavy_b_{s}", f"heavy_c_{s}") + "\n# edited\n"
    )
    deploy_workflow_worker(str(source))
    assert {g.kwargs["name"] for g in recorded} == first
    assert (f"heavy_a_{s}", f"heavy_c_{s}") in _groups_by_tasks(recorded)

    recorded.clear()
    source.write_text(source.read_text().replace("max_replicas=3", "max_replicas=4"))
    deploy_workflow_worker(str(source))
    second = {g.kwargs["name"] for g in recorded}
    assert len(first - second) == 2  # the two groups that use `heavy`
    assert "wf-ranker" in second


def test_tasks_without_image_or_resources_stay_on_the_main_worker(
    tmp_path: Path, recorded: list[_RecordingScalingGroup]
) -> None:
    source, _ = _write_tasks_script(
        tmp_path,
        "from chalk.workflows import task, workflow\n\n"
        "@task\ndef plain_SUFFIX(x: int) -> int:\n    return x\n\n"
        "@workflow\nasync def wf_SUFFIX() -> None:\n    pass\n",
    )
    worker = deploy_workflow_worker(str(source))
    (main,) = recorded
    assert main.kwargs["env"] is None
    assert main.kwargs["entrypoint"][4:] == []
    assert worker.task_workers == {}


@requires_task_resources
def test_imported_tasks_are_routed_and_unrelated_ones_skipped(
    tmp_path: Path,
    recorded: list[_RecordingScalingGroup],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import types
    import uuid

    from chalk.workflows import TaskResources, task

    s = uuid.uuid4().hex[:8]
    for module in ("shared", "other"):
        (tmp_path / f"{module}_{s}.py").write_text(
            "from chalk.workflows import TaskResources, task\n"
            "@task(resources=TaskResources(max_concurrent_tasks=1))\n"
            f"def {module}_{s}(x: int) -> int:\n    return x\n"
        )
    source = tmp_path / f"flow_{s}.py"
    source.write_text(
        f"import other_{s}\nfrom shared_{s} import shared_{s}\n"
        "from chalk.workflows import workflow\n"
        f"@workflow\nasync def wf_{s}() -> None:\n    pass\n"
    )

    @task(name=f"unrelated_{s}", resources=TaskResources(cpu=1))
    def unrelated(x: int) -> int:  # pyright: ignore[reportUnusedFunction]
        return x

    with caplog.at_level("WARNING"):
        worker = deploy_workflow_worker(str(source))
    assert set(worker.task_workers) == {f"shared_{s}", f"other_{s}"}
    assert f"unrelated_{s}" not in caplog.text

    # Deployed again, with the imported modules already loaded.
    recorded.clear()
    worker = deploy_workflow_worker(str(source))
    assert set(worker.task_workers) == {f"shared_{s}", f"other_{s}"}

    # As the running script, tasks registered by other code are skipped with a warning.
    recorded.clear()
    main = types.ModuleType("__main__")
    main.__file__ = str(source)
    exec(compile(source.read_text(), str(source), "exec"), vars(main))
    monkeypatch.setitem(sys.modules, "__main__", main)
    with caplog.at_level("WARNING"):
        worker = deploy_workflow_worker()
    assert set(worker.task_workers) == {f"shared_{s}", f"other_{s}"}
    assert (
        f"'unrelated_{s}' has an image or resources but is not defined" in caplog.text
    )


@requires_task_resources
def test_task_image_must_be_an_image(
    tmp_path: Path, recorded: list[_RecordingScalingGroup]
) -> None:
    source, _ = _write_tasks_script(
        tmp_path,
        "from chalk.workflows import task, workflow\n\n"
        "@task(image=42)\ndef odd_SUFFIX(x: int) -> int:\n    return x\n\n"
        "@workflow\nasync def wf_SUFFIX() -> None:\n    pass\n",
    )
    with pytest.raises(TypeError, match="image must be a chalkcompute.Image"):
        deploy_workflow_worker(str(source))
    assert recorded == []


def test_stale_task_workers_are_deleted_by_tag_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tags = {
        "wf-ranker-aaaaaaaa": {"chalk.workflow-worker": "wf-ranker"},
        "wf-ranker-bbbbbbbb": {"chalk.workflow-worker": "wf-ranker"},
        # Another worker whose name happens to look like one of ours.
        "wf-ranker-cccccccc": {"chalk.workflow-worker": "wf-ranker-cccccccc"},
        "wf-ranker-dddddddd": {},
        "wf-ranker": {},
    }
    deleted: list[str] = []

    class _Group:
        def __init__(self, name: str) -> None:
            self.name = name

        def delete(self) -> None:
            if self.name == "wf-ranker-bbbbbbbb":
                raise RuntimeError("API down")
            deleted.append(self.name)

    class _FakeScalingGroup:
        @staticmethod
        def from_name(name: str, **_: Any) -> _Group:
            return _Group(name)

    monkeypatch.setattr(workflow_worker_mod, "ScalingGroup", _FakeScalingGroup)
    monkeypatch.setattr(
        workflow_worker_mod,
        "_list_scaling_group_names",
        lambda search, client: [n for n in tags if n.startswith(search)],
    )
    monkeypatch.setattr(
        workflow_worker_mod, "_scaling_group_tags", lambda name, client: tags[name]
    )

    # A failed delete is logged, not raised: the deploy itself succeeded.
    result = workflow_worker_mod._delete_stale_task_workers(
        "wf-ranker", keep={"wf-ranker-aaaaaaaa"}, chalk_client=None
    )
    assert result == deleted == []

    tags["wf-ranker-eeeeeeee"] = {"chalk.workflow-worker": "wf-ranker"}
    result = workflow_worker_mod._delete_stale_task_workers(
        "wf-ranker", keep={"wf-ranker-aaaaaaaa"}, chalk_client=None
    )
    assert result == deleted == ["wf-ranker-eeeeeeee"]


def test_stale_task_worker_lookup_failure_does_not_fail_the_deploy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(search: str, client: Any) -> list[str]:
        raise RuntimeError("API down")

    monkeypatch.setattr(workflow_worker_mod, "_list_scaling_group_names", fail)
    assert (
        workflow_worker_mod._delete_stale_task_workers(
            "wf-ranker", keep=set(), chalk_client=None
        )
        == []
    )


def test_deploy_without_chalkpy_task_workers_routes_nothing(
    tmp_path: Path,
    recorded: list[_RecordingScalingGroup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "chalk.workflows.worker", None)
    source = tmp_path / "nightly_scoring.py"
    source.write_text(_SCRIPT)

    worker = deploy_workflow_worker(str(source))

    (main,) = recorded
    assert main.kwargs["env"] is None
    assert worker.task_workers == {}


def test_max_concurrent_tasks_needs_a_chalkpy_that_knows_the_flag(
    tmp_path: Path,
    recorded: list[_RecordingScalingGroup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "nightly_scoring.py"
    source.write_text(_SCRIPT)
    monkeypatch.setattr(
        workflow_worker_mod, "_chalkpy_has_task_resources", lambda: True
    )
    deploy_workflow_worker(str(source), max_concurrent_tasks=1)
    assert recorded[-1].kwargs["entrypoint"][-2:] == ["--max-concurrent-tasks", "1"]

    recorded.clear()
    monkeypatch.setattr(
        workflow_worker_mod, "_chalkpy_has_task_resources", lambda: False
    )
    with pytest.raises(RuntimeError, match="upgrade chalkpy"):
        deploy_workflow_worker(str(source), max_concurrent_tasks=1)
    assert recorded == []
