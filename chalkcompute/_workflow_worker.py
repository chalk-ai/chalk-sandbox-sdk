"""Run a local file's ``chalk.workflows`` workflows on a scale-to-zero worker.

``run_workflow(my_workflow, {...})``, called from a script started with ``uv run``,
bundles the script into a scaling group whose replicas poll a task queue of their
own, starts the workflow on that queue, and waits for its result. The environment's
workflow orchestrator drives the run: the dataplane scales the group up while any
workflow is open on the queue and back to zero once none are, so a workflow that
sleeps, retries, or outlives the script still finishes.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
import sys
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from chalkcompute._image import Image
from chalkcompute._naming import sanitize_dns_label
from chalkcompute._scaling_group import ScalingGroup

if TYPE_CHECKING:
    from chalk.workflows import WorkflowSchedule

    from chalkcompute._secret import Secret

logger = logging.getLogger(__name__)

# Queues outside the `chalk-workflows` family, so the offline-query consumer's workflow
# supervisor (which starts workers for `chalk-workflows.<deployment>` queues) never claims them.
_TASK_QUEUE_PREFIX = "chalkcompute-workflows."

# Room for the environment-id prefix a scaling group's hostname label carries.
_MAX_WORKER_NAME_LENGTH = 40

_SOURCE_DIR = "/app"

# Older chalkpy reports a schedule call's HTTP status only in its message, as "<Method> failed (<status>): ...";
# a server without the schedule service answers 404 or 501.
_SCHEDULES_UNSUPPORTED_RE = re.compile(r"^\w+ failed \((404|501)\): ")

# PEP 723 inline script metadata, e.g. the `# /// script` block `uv run` reads.
_SCRIPT_METADATA_RE = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)


@dataclass(frozen=True)
class WorkflowWorker:
    """A deployed worker serving one source file's workflows from its own task queue."""

    scaling_group: ScalingGroup
    task_queue: str
    chalk_client: Any | None = field(default=None, repr=False, compare=False)
    """The client the worker was deployed with; ``trigger`` and ``delete`` use it by default."""

    def trigger(
        self,
        workflow: Any,
        input: Mapping[str, Any] | None = None,
        *,
        workflow_id: str | None = None,
        wait: bool = False,
        chalk_client: Any | None = None,
    ) -> Any:
        """Start ``workflow`` on this worker's queue.

        Returns a run handle, or the workflow's result when ``wait`` is true.
        """
        client = self._client(chalk_client)
        return client.trigger_workflow(
            workflow,
            input,
            workflow_id=workflow_id,
            wait=wait,
            task_queue=self.task_queue,
        )

    def delete(self, *, chalk_client: Any | None = None) -> None:
        """Delete the worker's workflow schedules, then its scaling group.

        Schedules go first so that no firing starts a run on a queue with no worker. On a
        chalkpy or Chalk server without workflow schedules there are none to delete, so the
        scaling group is deleted alone.
        """
        sync = getattr(self._client(chalk_client), "sync_workflow_schedules", None)
        if sync is not None:
            try:
                sync(self.task_queue, [])
            except Exception as exc:
                if not _schedules_unsupported(exc):
                    raise
                logger.warning(
                    "Not deleting workflow schedules for %s: the Chalk server does not support them (%s)",
                    self.task_queue,
                    exc,
                )
        self.scaling_group.delete()

    def _client(self, chalk_client: Any | None) -> Any:
        if chalk_client is not None:
            return chalk_client
        if self.chalk_client is not None:
            return self.chalk_client
        return _default_chalk_client()


def _schedules_unsupported(exc: Exception) -> bool:
    # chalkpy's WorkflowScheduleError carries the Connect code, "unimplemented" when the server lacks schedules.
    if getattr(exc, "code", None) == "unimplemented":
        return True
    return bool(_SCHEDULES_UNSUPPORTED_RE.match(str(exc)))


def script_dependencies(source: str) -> list[str] | None:
    """Return the ``dependencies`` of a PEP 723 ``# /// script`` block, or None if absent."""
    import tomllib

    for match in _SCRIPT_METADATA_RE.finditer(source):
        if match.group("type") != "script":
            continue
        content = "".join(
            line[2:] if line.startswith("# ") else line[1:]
            for line in match.group("content").splitlines(keepends=True)
        )
        return list(tomllib.loads(content).get("dependencies", []))
    return None


def _requirement_name(requirement: str) -> str:
    return (
        re.split(r"[\s\[<>=!~;@]", requirement.strip(), maxsplit=1)[0]
        .lower()
        .replace("_", "-")
    )


def worker_requirements(source: str) -> list[str]:
    """Packages the worker image installs for ``source``.

    A script's own inline dependencies are what ``uv run`` gave it locally, so they are
    what its worker gets too. ``chalkpy[workflows]`` is always added: the worker process
    is chalkpy's, and it needs that extra even when the script only lists chalkpy.
    """
    declared = script_dependencies(source)
    if declared is None:
        declared = [f"chalkpy=={_installed_version('chalkpy')}"]
        if re.search(r"(?m)^\s*(import|from)\s+chalkcompute\b", source):
            declared.append(f"chalkcompute=={_installed_version('chalkcompute')}")
    chalkpy = [r for r in declared if _requirement_name(r) == "chalkpy"]
    others = [r for r in declared if _requirement_name(r) != "chalkpy"]
    if not chalkpy:
        return ["chalkpy[workflows]", *others]
    # Re-state each chalkpy requirement with the extra, keeping its version constraint.
    return [
        re.sub(r"^(chalkpy)(\[[^\]]*\])?", r"chalkpy[workflows]", r.strip(), flags=re.I)
        for r in chalkpy
    ] + others


def _installed_version(distribution: str) -> str:
    from importlib.metadata import version

    return version(distribution)


def default_worker_name(source_path: str) -> str:
    stem = os.path.splitext(os.path.basename(source_path))[0]
    name = sanitize_dns_label(f"wf-{stem}", max_length=_MAX_WORKER_NAME_LENGTH)
    if not name:
        raise ValueError(
            f"Cannot derive a worker name from {source_path!r}; pass name=..."
        )
    return name


def _source_path_for(workflow: Any) -> str:
    fn = getattr(workflow, "fn", None)
    if fn is None:
        raise TypeError(f"Expected a @workflow-decorated function, got {workflow!r}")
    return os.path.abspath(inspect.getfile(inspect.unwrap(fn)))


def _default_chalk_client() -> Any:
    from chalk.client import ChalkClient

    return ChalkClient()


def deploy_workflow_worker(
    source: str | None = None,
    *,
    name: str | None = None,
    image: Image | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    env: Mapping[str, str | Secret] | None = None,
    secrets: list[Secret] | None = None,
    max_replicas: int = 1,
    shutdown_delay_seconds: int | None = None,
    schedules: Sequence[WorkflowSchedule] | None = None,
    chalk_client: Any | None = None,
) -> WorkflowWorker:
    """Deploy a scale-to-zero worker for the workflows defined in ``source``.

    The group starts at zero replicas. It scales up while the orchestrator has any
    workflow open on the worker's task queue and back to zero after
    ``shutdown_delay_seconds`` without one.

    Parameters
    ----------
    source
        Python file defining the ``@workflow``/``@task`` functions. Defaults to the
        running script.
    name
        Scaling group name. Defaults to ``wf-<file stem>``; redeploying the same file
        updates the same worker.
    image
        Base image for the worker. The source file is added to it, along with the
        packages from ``worker_requirements`` when no image is given.
    schedules
        ``chalk.workflows.WorkflowSchedule`` cadences that start the file's workflows on
        this worker. After deploying, the worker's task queue is synced to exactly these
        schedules: new ones are created, changed ones updated, and ones no longer listed
        deleted, while a paused schedule stays paused. ``None`` leaves the queue's
        schedules as they are; ``[]`` deletes them all. A schedule's workflow must be
        defined in ``source`` or imported by it from a package in the worker's image;
        deploying warns about a workflow definition from any other file. Set
        ``execution_timeout`` on ``"skip"`` schedules: a run that never finishes blocks
        every later firing. Schedule names are unique within an environment and default
        to the workflow name, so pass ``name=...`` when another worker already schedules
        a workflow with the same name; otherwise the sync fails after the worker deploys.
    """
    if schedules is not None:
        schedules = _validated_schedules(schedules)
    if source is None:
        source = getattr(sys.modules.get("__main__"), "__file__", None)
        if source is None:
            raise ValueError("No running script to deploy; pass source=...")
    source = os.path.abspath(source)
    if schedules is not None:
        _check_schedules_run_on(schedules, source)
    with open(source, encoding="utf-8") as f:
        text = f.read()

    worker_name = name if name is not None else default_worker_name(source)
    task_queue = f"{_TASK_QUEUE_PREFIX}{worker_name}"
    remote_path = f"{_SOURCE_DIR}/{os.path.basename(source)}"

    if image is None:
        python = f"{sys.version_info.major}.{sys.version_info.minor}"
        image = Image.debian_slim(python).pip_install(worker_requirements(text))
    image = image.add_local_file(source, remote_path)

    scaling_group = ScalingGroup(
        image=image,
        name=worker_name,
        cpu=cpu,
        memory=memory,
        env=env,
        secrets=secrets,
        # The worker polls its task queue rather than serving requests; the orchestrator address and the
        # task queue arrive as CHALK_WORKFLOW_ORCHESTRATOR_* env vars set by the platform.
        entrypoint=["python", "-m", "chalk.workflows.worker", remote_path],
        port=None,
        min_replicas=0,
        max_replicas=max_replicas,
        shutdown_delay_seconds=shutdown_delay_seconds,
        workflow_task_queue=task_queue,
        chalk_client=chalk_client,
    ).deploy()
    client = chalk_client if chalk_client is not None else _default_chalk_client()
    worker = WorkflowWorker(
        scaling_group=scaling_group, task_queue=task_queue, chalk_client=client
    )
    if schedules is not None:
        try:
            client.sync_workflow_schedules(task_queue, schedules)
        except Exception as exc:
            raise RuntimeError(
                f"Worker {worker_name} was deployed but its schedules were not synced: {exc}; redeploy to retry"
            ) from exc
    return worker


def _validated_schedules(
    schedules: Sequence[WorkflowSchedule],
) -> list[WorkflowSchedule]:
    try:
        from chalk.workflows import WorkflowSchedule
    except ImportError as exc:
        raise ImportError(
            "Workflow schedules need chalkpy with workflow schedule support: "
            + "pip install -U 'chalkpy[workflows]'"
        ) from exc
    if isinstance(schedules, (str, WorkflowSchedule)):
        raise TypeError("schedules must be a list of WorkflowSchedule")
    result = list(schedules)
    for schedule in result:
        if not isinstance(schedule, WorkflowSchedule):
            raise TypeError(f"Expected a WorkflowSchedule, got {schedule!r}")
    # The queue's schedules are matched by name, so two with the same name would fail the sync after deploying.
    names = [schedule.schedule_name for schedule in result]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(
            f"Workflow schedule names must be unique; pass name=... to tell apart {duplicates}"
        )
    return result


def _check_schedules_run_on(schedules: Sequence[WorkflowSchedule], source: str) -> None:
    for schedule in schedules:
        # A run of a workflow the worker doesn't define is retried forever. The worker serves every
        # workflow its source registers, including ones imported from packages in its image, so a
        # definition from another file is only suspect; a workflow given by name is taken on trust.
        if not isinstance(schedule.workflow, str):
            defined_in = _source_path_for(schedule.workflow)
            if os.path.realpath(defined_in) != os.path.realpath(source):
                warnings.warn(
                    f"Schedule {schedule.schedule_name!r} starts workflow {schedule.workflow_name!r}, "
                    + f"which is defined in {defined_in}, not in the worker's source {source}; "
                    + "unless the source imports it from a package in the worker's image, its runs "
                    + "will never start",
                    stacklevel=3,
                )
        if schedule.overlap_policy == "skip" and schedule.execution_timeout is None:
            warnings.warn(
                f"Schedule {schedule.schedule_name!r} skips firings while a run is open and has no "
                + "execution_timeout, so a run that never finishes blocks it forever; "
                + "set execution_timeout=... to bound each run",
                stacklevel=3,
            )


def run_workflow(
    workflow: Any,
    input: Mapping[str, Any] | None = None,
    *,
    workflow_id: str | None = None,
    wait: bool = True,
    chalk_client: Any | None = None,
    **deploy_kwargs: Any,
) -> Any:
    """Deploy a worker for the file defining ``workflow``, then start it there.

    Blocks until the workflow finishes and returns its result, unless ``wait`` is
    false, in which case it returns a run handle as soon as the run is accepted.
    Extra keyword arguments go to ``deploy_workflow_worker``.
    """
    worker = deploy_workflow_worker(
        _source_path_for(workflow),
        chalk_client=chalk_client,
        **deploy_kwargs,
    )
    return worker.trigger(
        workflow,
        input,
        workflow_id=workflow_id,
        wait=wait,
        chalk_client=chalk_client,
    )
