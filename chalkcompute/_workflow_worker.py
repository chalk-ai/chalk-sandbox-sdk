"""Run a local file's ``chalk.workflows`` workflows on a scale-to-zero worker.

``run_workflow(my_workflow, {...})``, called from a script started with ``uv run``,
bundles the script into a scaling group whose replicas poll a task queue of their
own, starts the workflow on that queue, and waits for its result. The environment's
workflow orchestrator drives the run: the dataplane scales the group up while any
workflow is open on the queue and back to zero once none are, so a workflow that
sleeps, retries, or outlives the script still finishes.

A task declared with ``@task(image=...)`` runs on a worker of its own, one per distinct
image, which polls its own queue and scales the same way: the workflow calls the task as
a child workflow on that queue, so the image's group is up only while such a call is open.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

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

# Where the worker running the workflows sends each task that has an image of its own;
# read by `chalk.workflows.worker`.
_TASK_QUEUES_ENV_VAR = "CHALK_WORKFLOW_TASK_QUEUES"

# Hex digits of the image hash in a task worker's name.
_IMAGE_KEY_LENGTH = 8

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
    """The group serving each task that has an image of its own, by task name."""

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
        declared = [_installed_requirement("chalkpy")]
        if re.search(r"(?m)^\s*(import|from)\s+chalkcompute\b", source):
            declared.append(_installed_requirement("chalkcompute"))
    chalkpy = [r for r in declared if _requirement_name(r) == "chalkpy"]
    others = [r for r in declared if _requirement_name(r) != "chalkpy"]
    if not chalkpy:
        return ["chalkpy[workflows]", *others]
    # Re-state each chalkpy requirement with the extra, keeping its other extras and
    # its version constraint.
    return [_with_extra(r, "workflows") for r in chalkpy] + others


_REQUIREMENT_EXTRAS_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9._-]+)(?:\[(?P<extras>[^\]]*)\])?(?P<rest>.*)$", re.S
)


def _with_extra(requirement: str, extra: str) -> str:
    match = _REQUIREMENT_EXTRAS_RE.match(requirement.strip())
    assert match is not None
    extras = [e.strip() for e in (match.group("extras") or "").split(",") if e.strip()]
    if extra not in extras:
        extras.append(extra)
    return f"{match.group('name')}[{','.join(extras)}]{match.group('rest')}"


def _installed_requirement(distribution: str) -> str:
    """``distribution`` pinned to the version installed here, so the worker runs what the
    script ran against. A checkout installed with ``uv pip install -e`` reports the
    placeholder ``0.0.0``, which no index serves, so it gets the latest release instead."""
    version = _installed_version(distribution)
    if _is_placeholder_version(version):
        return distribution
    return f"{distribution}=={version}"


def _is_placeholder_version(version: str) -> bool:
    return re.fullmatch(r"0(\.0)*([.+-].*)?", version.strip()) is not None


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


def _task_images(source: str) -> dict[str, Image]:
    """The registered tasks that declare an image, by task name.

    The running script's tasks are already registered; any other file is imported,
    as its worker will import it, so that its decorators run. Tasks that ``source``
    imports from other modules count too: the worker running the workflows routes
    by task name, so they must be served somewhere, and the task's worker imports
    ``source`` just as the main worker does.
    """
    from chalk.workflows import TASK_REGISTRY, WORKFLOW_REGISTRY
    from chalk.workflows.worker import load_workflow_source

    def defined_here(definition: Any) -> bool:
        return os.path.abspath(definition.filename) == source

    if not any(
        defined_here(d) for d in (*WORKFLOW_REGISTRY.values(), *TASK_REGISTRY.values())
    ):
        load_workflow_source(source)
    images: dict[str, Image] = {}
    for definition in TASK_REGISTRY.values():
        image = getattr(definition, "image", None)
        if image is None:
            continue
        if isinstance(image, str):
            image = Image.base(image)
        if not isinstance(image, Image):
            raise TypeError(
                f"@task {definition.name!r}: image must be a chalkcompute.Image or an image reference, "
                f"got {type(image).__name__}"
            )
        images[definition.name] = image
    return images


def image_key(image: Image) -> str:
    """A short hash identifying ``image``: tasks with equal images share a worker, and
    a redeploy of the same image reuses its worker's name."""
    identity = {
        # Canonical JSON rather than proto bytes, which leave map (env) order unspecified.
        "spec": json.loads(image._to_build_proto().to_json()),
        # Contents, not local paths: the same image from another checkout or machine
        # is the same image.
        "local_files": sorted(
            [_file_digest(f.src), f.dest, f.mode, f.mount_root]
            for f in image.lazy_local_files
        ),
        "volumes": sorted(
            [m.name, m.mount_path, m.version_id] for m in image._volume_mounts
        ),
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode())
    return digest.hexdigest()[:_IMAGE_KEY_LENGTH]


def _file_digest(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except FileNotFoundError:
        # Reported when the image is uploaded; the key only needs to be stable.
        return f"missing:{path}"


def task_worker_name(worker_name: str, key: str) -> str:
    prefix = worker_name[: _MAX_WORKER_NAME_LENGTH - len(key) - 1]
    return sanitize_dns_label(f"{prefix}-{key}", max_length=_MAX_WORKER_NAME_LENGTH)


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

    Each distinct ``@task(image=...)`` image gets a group of its own,
    ``<name>-<image hash>``, deployed with the same resources, env, and secrets. Its
    image is the task's, plus the packages from ``worker_requirements`` and the
    source file, since that worker imports the file too. Tasks ``source`` imports
    from other modules are included; those modules must be present in the task's
    image, as they must be in the main worker's. Task workers left over from an
    earlier deploy of this file, for images it no longer uses, are deleted.
    """
    if source is None:
        source = getattr(sys.modules.get("__main__"), "__file__", None)
        if source is None:
            raise ValueError("No running script to deploy; pass source=...")
    source = os.path.abspath(source)
    with open(source, encoding="utf-8") as f:
        text = f.read()

    worker_name = name if name is not None else default_worker_name(source)
    task_queue = f"{_TASK_QUEUE_PREFIX}{worker_name}"
    remote_path = f"{_SOURCE_DIR}/{os.path.basename(source)}"

    requirements = worker_requirements(text)
    if image is None:
        python = f"{sys.version_info.major}.{sys.version_info.minor}"
        image = Image.debian_slim(python).pip_install(requirements)
    image = image.add_local_file(source, remote_path)

    def deploy_group(
        group_name: str,
        group_image: Image,
        group_queue: str,
        group_env: Mapping[str, str | Secret] | None,
        worker_args: list[str],
    ) -> ScalingGroup:
        return ScalingGroup(
            image=group_image,
            name=group_name,
            cpu=cpu,
            memory=memory,
            env=group_env,
            secrets=secrets,
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
            max_replicas=max_replicas,
            shutdown_delay_seconds=shutdown_delay_seconds,
            workflow_task_queue=group_queue,
            chalk_client=chalk_client,
        ).deploy()

    tasks_by_image: dict[str, list[str]] = {}
    images_by_key: dict[str, Image] = {}
    for task_name, task_image in _task_images(source).items():
        key = image_key(task_image)
        tasks_by_image.setdefault(key, []).append(task_name)
        images_by_key[key] = task_image

    # Task workers first, so the workflows never route a task to a queue nothing will serve.
    task_workers: dict[str, ScalingGroup] = {}
    task_queues: dict[str, str] = {}
    task_worker_names: list[str] = []
    for key, task_names in tasks_by_image.items():
        group_name = task_worker_name(worker_name, key)
        task_worker_names.append(group_name)
        group_queue = f"{_TASK_QUEUE_PREFIX}{group_name}"
        group = deploy_group(
            group_name,
            images_by_key[key]
            .pip_install(requirements)
            .add_local_file(source, remote_path),
            group_queue,
            env,
            ["--tasks", ",".join(task_names)],
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
    scaling_group = deploy_group(worker_name, image, task_queue, main_env, [])
    # Only now, with the main group routing nothing to them, are the task workers of
    # images this file no longer uses safe to remove.
    _delete_stale_task_workers(
        worker_name, keep=set(task_worker_names), chalk_client=chalk_client
    )
    return WorkflowWorker(
        scaling_group=scaling_group, task_queue=task_queue, task_workers=task_workers
    )


def _task_worker_name_pattern(worker_name: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(worker_name)}-[0-9a-f]{{{_IMAGE_KEY_LENGTH}}}$")


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
    """Delete ``<worker_name>-<image hash>`` groups other than ``keep``: task workers
    an earlier deploy of this file created for images it no longer uses."""
    pattern = _task_worker_name_pattern(worker_name)
    try:
        names = _list_scaling_group_names(worker_name, chalk_client)
    except Exception as exc:  # a failed cleanup must not fail the deploy
        logger.warning(
            "Could not list scaling groups to clean up task workers: %s", exc
        )
        return []
    stale = sorted(n for n in set(names) if pattern.match(n) and n not in keep)
    deleted: list[str] = []
    for name in stale:
        try:
            ScalingGroup.from_name(name, chalk_client=chalk_client).delete()
        except Exception as exc:
            logger.warning("Could not delete stale task worker %r: %s", name, exc)
            continue
        logger.info("Deleted stale task worker %r", name)
        deleted.append(name)
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
