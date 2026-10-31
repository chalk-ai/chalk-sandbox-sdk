"""Run a local file's ``chalk.workflows`` workflows on a scale-to-zero worker.

``run_workflow(my_workflow, {...})``, called from a script started with ``uv run``,
bundles the script into a scaling group whose replicas poll a task queue of their
own, starts the workflow on that queue, and waits for its result. The environment's
workflow orchestrator drives the run: the dataplane scales the group up while any
workflow is open on the queue and back to zero once none are, so a workflow that
sleeps, retries, or outlives the script still finishes.

A task declared with ``@task(image=...)`` or ``@task(resources=TaskResources(...))`` runs
on a worker of its own, one per distinct image and resources, which polls its own queue
and scales the same way: the workflow calls the task as a child workflow on that queue,
so the task's group is up only while such a call is open.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import sys
import types
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

from chalkcompute._chalk_identity import ChalkIdentity
from chalkcompute._image import Image
from chalkcompute._naming import sanitize_dns_label
from chalkcompute._scaling_group import ScalingGroup

if TYPE_CHECKING:
    from chalkcompute._secret import Secret

# Queues outside the `chalk-workflows` family, so the offline-query consumer's workflow
# supervisor (which starts workers for `chalk-workflows.<deployment>` queues) never claims them.
_TASK_QUEUE_PREFIX = "chalkcompute-workflows."

# Room for the environment-id prefix a scaling group's hostname label carries.
_MAX_WORKER_NAME_LENGTH = 40

_SOURCE_DIR = "/app"

logger = logging.getLogger(__name__)

# Where the worker running the workflows sends each task that has a worker of its own;
# read by `chalk.workflows.worker`.
_TASK_QUEUES_ENV_VAR = "CHALK_WORKFLOW_TASK_QUEUES"

# Hex digits of the image-and-resources hash in a task worker's name.
_TASK_WORKER_KEY_LENGTH = 8

# Tag on a task worker naming the workflow worker it belongs to. Cleanup of task workers
# goes by this tag, never by name alone: another worker's name may look like one.
_TASK_WORKER_TAG = "chalk.workflow-worker"

# The task-queue trigger wakes a group to one replica while any workflow is open on its
# queue; it does not count them. Past one replica, a task worker scales on CPU.
_TASK_WORKER_TARGET_CPU_UTILIZATION = 60

# PEP 723 inline script metadata, e.g. the `# /// script` block `uv run` reads.
_SCRIPT_METADATA_RE = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)


@dataclass(frozen=True)
class WorkflowWorker:
    """A deployed worker serving one source file's workflows from its own task queue."""

    scaling_group: ScalingGroup
    task_queue: str
    task_workers: Mapping[str, ScalingGroup] = field(default_factory=dict)
    """The group serving each task that has a worker of its own, by task name."""

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
        client = chalk_client if chalk_client is not None else _default_chalk_client()
        return client.trigger_workflow(
            workflow,
            input,
            workflow_id=workflow_id,
            wait=wait,
            task_queue=self.task_queue,
        )


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


_RUNTIME_DISTRIBUTIONS = ("chalkpy", "chalkcompute")


def task_worker_requirements(source: str) -> list[str]:
    """Packages a task's own image installs on top of what the task declared.

    Only the runtime, ``chalkpy[workflows]`` and (when the file imports it)
    ``chalkcompute``, at the script's constraints: the point of a task image is to take
    different dependencies from the script, so the script's other packages stay out.
    """
    return [
        r
        for r in worker_requirements(source)
        if _requirement_name(r) in _RUNTIME_DISTRIBUTIONS
    ]


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


@dataclass(frozen=True)
class _TaskWorkerSpec:
    """How a task's own worker is built and sized; tasks with equal specs share one."""

    image: Image | None
    """The task's image, or None to run on the workflow worker's image."""
    cpu: str | None
    memory: str | None
    max_replicas: int | None
    max_concurrent_tasks: int | None


def _routed_tasks(source: str) -> dict[str, _TaskWorkerSpec]:
    """The tasks ``source`` uses that get a worker of their own, by task name: those with
    an image or resources among the tasks it defines or imports, since the worker running
    its workflows routes them by name and the task's worker imports ``source`` too.

    The running script's module is read as it is. Any other file is imported again, as
    its worker will import it, so a file edited since an earlier deploy in this process
    is read as it is now.
    """
    try:
        from chalk.workflows import TASK_REGISTRY
        from chalk.workflows.worker import (  # ty: ignore[unresolved-import]
            load_workflow_source,
        )
    except ImportError:
        # A chalkpy without the worker module has no per-task workers to route to.
        return {}

    main = sys.modules.get("__main__")
    main_file = getattr(main, "__file__", None)
    if main is not None and main_file and os.path.abspath(main_file) == source:
        module = main
        # Anything else in the process may have registered tasks too.
        candidates = set(TASK_REGISTRY)
    else:
        before = dict(TASK_REGISTRY)
        module = sys.modules[load_workflow_source(source)]
        candidates = {
            name
            for name, definition in TASK_REGISTRY.items()
            if before.get(name) is not definition
        }
    used = _tasks_used_by(module)
    # A module the file imports may already be loaded, and its tasks registered before.
    candidates |= {d.name for d in used}

    specs: dict[str, _TaskWorkerSpec] = {}
    for name in sorted(candidates):
        definition = TASK_REGISTRY[name]
        image = getattr(definition, "image", None)
        # Older chalkpy has no task resources.
        resources = getattr(definition, "resources", None)
        if image is None and resources is None:
            continue
        if definition not in used:
            logger.warning(
                "@task %r has an image or resources but is not defined or imported by %s; "
                "it gets no worker from this deploy",
                name,
                source,
            )
            continue
        if isinstance(image, str):
            image = Image.base(image)
        if image is not None and not isinstance(image, Image):
            raise TypeError(
                f"@task {name!r}: image must be a chalkcompute.Image or an image reference, "
                f"got {type(image).__name__}"
            )
        specs[name] = _TaskWorkerSpec(
            image=image,
            cpu=getattr(resources, "cpu", None),
            memory=getattr(resources, "memory", None),
            max_replicas=getattr(resources, "max_replicas", None),
            max_concurrent_tasks=getattr(resources, "max_concurrent_tasks", None),
        )
    return specs


def _tasks_used_by(module: types.ModuleType) -> list[Any]:
    """The task definitions bound in ``module``, directly or as attributes of a module it
    imports: what it defines, ``from x import task``, and ``import x`` then ``x.task``."""
    from chalk.workflows import TaskDefinition

    found: list[Any] = []
    for value in vars(module).values():
        values = (
            vars(value).values() if isinstance(value, types.ModuleType) else (value,)
        )
        found.extend(v for v in values if isinstance(v, TaskDefinition))
    return found


def image_key(image: Image) -> str:
    """A hash identifying ``image``: equal images give equal keys, even as separate objects
    or from another checkout, so a redeploy of the same image reuses its worker's name."""
    identity = {
        # Canonical JSON rather than proto bytes, which leave map (env) order unspecified.
        "spec": json.loads(image._to_build_proto().to_json()),
        "local_files": sorted(
            [_file_digest(f.src), f.dest, f.mode, f.mount_root]
            for f in image.lazy_local_files
        ),
        "volumes": sorted(
            [m.name, m.mount_path, m.version_id] for m in image._volume_mounts
        ),
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, default=str).encode()
    ).hexdigest()


def _file_digest(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except FileNotFoundError:
        # Reported when the image is uploaded; the key only needs to be stable.
        return f"missing:{path}"


def _task_worker_key(spec: _TaskWorkerSpec, worker_image_key: str) -> str:
    identity = [
        image_key(spec.image) if spec.image is not None else worker_image_key,
        spec.cpu,
        spec.memory,
        spec.max_replicas,
        spec.max_concurrent_tasks,
    ]
    digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return digest[:_TASK_WORKER_KEY_LENGTH]


def task_worker_name(worker_name: str, key: str) -> str:
    prefix = worker_name[: _MAX_WORKER_NAME_LENGTH - len(key) - 1]
    return sanitize_dns_label(f"{prefix}-{key}", max_length=_MAX_WORKER_NAME_LENGTH)


def _chalkpy_has_task_resources() -> bool:
    try:
        import chalk.workflows
    except ImportError:
        return False
    return hasattr(chalk.workflows, "TaskResources")


def _worker_args(max_concurrent_tasks: int | None) -> list[str]:
    if max_concurrent_tasks is None:
        return []
    # The worker image runs the chalkpy installed here, which must know the flag.
    if not _chalkpy_has_task_resources():
        raise RuntimeError(
            "max_concurrent_tasks needs a chalkpy with chalk.workflows.TaskResources; "
            "upgrade chalkpy[workflows]"
        )
    return ["--max-concurrent-tasks", str(max_concurrent_tasks)]


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
    max_concurrent_tasks: int | None = None,
    shutdown_delay_seconds: int | None = None,
    chalk_identity: bool | ChalkIdentity = False,
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
    max_concurrent_tasks
        Most task attempts one replica runs at once. Defaults to chalkpy's (16).
    chalk_identity
        Give the worker Chalk credentials that identify the principal deploying it,
        so ``ChalkClient()`` inside a task authenticates without a client id/secret.
        ``ChalkIdentity(mode="sandbox")`` is not supported for scaling groups.

    Each task with an ``image`` or ``resources`` of its own runs on a group per
    distinct image and resources, ``<name>-<hash>``, with the same env, secrets, and
    identity. Its image is the task's, plus the chalkpy runtime and the source file,
    or the worker's own image for a task with only resources; ``cpu``, ``memory``,
    ``max_replicas``, and ``max_concurrent_tasks`` come from the task's
    ``TaskResources`` where set, and from these arguments otherwise. Such a group
    wakes to one replica while any of its tasks is running or queued, and scales past
    one on CPU utilization. Task groups left over from an earlier deploy of this
    file are deleted.

    Redeploying a file whose task images or resources changed is not safe for its
    running workflows: the worker decides at each call where a task runs, so a run
    that replays after the routes changed fails as non-deterministic. Let runs finish,
    or start them again, after such a redeploy.
    """
    if source is None:
        source = getattr(sys.modules.get("__main__"), "__file__", None)
        if source is None:
            raise ValueError("No running script to deploy; pass source=...")
    source = os.path.abspath(source)
    with open(source, encoding="utf-8") as f:
        text = f.read()

    # Checked before anything is deployed.
    main_worker_args = _worker_args(max_concurrent_tasks)
    worker_name = name if name is not None else default_worker_name(source)
    task_queue = f"{_TASK_QUEUE_PREFIX}{worker_name}"
    remote_path = f"{_SOURCE_DIR}/{os.path.basename(source)}"

    if image is None:
        python = f"{sys.version_info.major}.{sys.version_info.minor}"
        image = Image.debian_slim(python).pip_install(worker_requirements(text))
    # Taken before the source file is added, so that editing the file does not rename
    # the groups of tasks that run on this image.
    worker_image_key = image_key(image)
    image = image.add_local_file(source, remote_path)

    def deploy_group(
        group_name: str,
        group_image: Image,
        group_queue: str,
        *,
        group_cpu: str | None,
        group_memory: str | None,
        group_max_replicas: int,
        group_env: Mapping[str, str | Secret] | None,
        worker_args: list[str],
        target_cpu_utilization_percentage: int | None = None,
        tags: dict[str, str] | None = None,
    ) -> ScalingGroup:
        return ScalingGroup(
            image=group_image,
            name=group_name,
            cpu=group_cpu,
            memory=group_memory,
            env=group_env,
            secrets=secrets,
            tags=tags,
            # The worker polls its task queue rather than serving requests; the orchestrator address and the
            # task queue arrive as CHALK_WORKFLOW_ORCHESTRATOR_* env vars set by the platform.
            entrypoint=[
                "python",
                "-m",
                "chalk.workflows.worker",
                remote_path,
                *worker_args,
            ],
            port=None,
            min_replicas=0,
            max_replicas=group_max_replicas,
            target_cpu_utilization_percentage=target_cpu_utilization_percentage,
            shutdown_delay_seconds=shutdown_delay_seconds,
            workflow_task_queue=group_queue,
            chalk_identity=chalk_identity,
            chalk_client=chalk_client,
        ).deploy()

    tasks_by_key: dict[str, list[str]] = {}
    specs_by_key: dict[str, _TaskWorkerSpec] = {}
    for task_name, spec in _routed_tasks(source).items():
        key = _task_worker_key(spec, worker_image_key)
        tasks_by_key.setdefault(key, []).append(task_name)
        specs_by_key[key] = spec

    # Task workers first, so the workflows never route a task to a queue nothing will serve.
    task_workers: dict[str, ScalingGroup] = {}
    task_queues: dict[str, str] = {}
    task_worker_names: set[str] = set()
    task_requirements = task_worker_requirements(text)
    for key, task_names in tasks_by_key.items():
        spec = specs_by_key[key]
        group_name = task_worker_name(worker_name, key)
        task_worker_names.add(group_name)
        group_queue = f"{_TASK_QUEUE_PREFIX}{group_name}"
        group_max_replicas = (
            spec.max_replicas if spec.max_replicas is not None else max_replicas
        )
        group = deploy_group(
            group_name,
            (
                spec.image.pip_install(task_requirements).add_local_file(
                    source, remote_path
                )
                if spec.image is not None
                else image
            ),
            group_queue,
            group_cpu=spec.cpu if spec.cpu is not None else cpu,
            group_memory=spec.memory if spec.memory is not None else memory,
            group_max_replicas=group_max_replicas,
            group_env=env,
            worker_args=[
                "--tasks",
                ",".join(sorted(task_names)),
                *_worker_args(
                    spec.max_concurrent_tasks
                    if spec.max_concurrent_tasks is not None
                    else max_concurrent_tasks
                ),
            ],
            target_cpu_utilization_percentage=(
                _TASK_WORKER_TARGET_CPU_UTILIZATION if group_max_replicas > 1 else None
            ),
            tags={_TASK_WORKER_TAG: worker_name},
        )
        for task_name in task_names:
            task_workers[task_name] = group
            task_queues[task_name] = group_queue

    main_env = env
    if task_queues:
        main_env = {
            **(env or {}),
            _TASK_QUEUES_ENV_VAR: json.dumps(task_queues, sort_keys=True),
        }
    scaling_group = deploy_group(
        worker_name,
        image,
        task_queue,
        group_cpu=cpu,
        group_memory=memory,
        group_max_replicas=max_replicas,
        group_env=main_env,
        worker_args=main_worker_args,
    )
    # Only now, with the main group routing nothing to them, are the task workers this
    # file no longer uses safe to remove.
    _delete_stale_task_workers(
        worker_name,
        keep=task_worker_names,
        chalk_client=chalk_client,
    )
    return WorkflowWorker(
        scaling_group=scaling_group, task_queue=task_queue, task_workers=task_workers
    )


def _scaling_group_tags(name: str, chalk_client: Any | None) -> dict[str, str]:
    from chalkcompute._scaling_group import _get_scaling_group

    client = ScalingGroup._make_empty(chalk_client=chalk_client)._client
    data = _get_scaling_group(client, name=name).scaling_group
    spec = getattr(data, "spec", None)
    container = getattr(spec, "container_spec", None)
    return dict(getattr(container, "tags", None) or {})


def _list_scaling_group_names(search: str, chalk_client: Any | None) -> list[str]:
    from chalkcompute._gen.chalk.scalinggroup.v1 import service_connect as sg_connect
    from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb

    client = ScalingGroup._make_empty(chalk_client=chalk_client)._client
    stub = client.stub(sg_connect.ScalingGroupManagerServiceClientSync)
    names: list[str] = []
    cursor: str | None = None
    while True:
        response = client.call(
            stub.list_scaling_groups,
            sg_pb.ListScalingGroupsRequest(search=search, limit=100, cursor=cursor),
        )
        names.extend(g.name for g in response.scaling_groups)
        cursor = response.next_cursor or None
        if cursor is None or not response.scaling_groups:
            return names


def _delete_stale_task_workers(
    worker_name: str, *, keep: set[str], chalk_client: Any | None
) -> list[str]:
    """Delete the task workers tagged as ``worker_name``'s other than ``keep``: those
    an earlier deploy of this file created for tasks it no longer routes the same way."""
    # The name prefix narrows the listing; the tag decides.
    prefix = task_worker_name(worker_name, "0" * _TASK_WORKER_KEY_LENGTH)[
        :-_TASK_WORKER_KEY_LENGTH
    ]
    try:
        names = _list_scaling_group_names(prefix, chalk_client)
        stale = sorted(
            n
            for n in set(names)
            if n.startswith(prefix)
            and n not in keep
            and n != worker_name
            and _scaling_group_tags(n, chalk_client).get(_TASK_WORKER_TAG)
            == worker_name
        )
    except Exception as exc:  # a failed cleanup must not fail the deploy
        logger.warning(
            "Could not look up task workers of %r to clean up: %s", worker_name, exc
        )
        return []
    deleted: list[str] = []
    for stale_name in stale:
        try:
            ScalingGroup.from_name(stale_name, chalk_client=chalk_client).delete()
        except Exception as exc:
            logger.warning("Could not delete stale task worker %r: %s", stale_name, exc)
            continue
        logger.info("Deleted stale task worker %r", stale_name)
        deleted.append(stale_name)
    return deleted


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
