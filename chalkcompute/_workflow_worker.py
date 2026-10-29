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
import os
import re
import sys
from dataclasses import dataclass
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

# PEP 723 inline script metadata, e.g. the `# /// script` block `uv run` reads.
_SCRIPT_METADATA_RE = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)


@dataclass(frozen=True)
class WorkflowWorker:
    """A deployed worker serving one source file's workflows from its own task queue."""

    scaling_group: ScalingGroup
    task_queue: str

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
    chalk_identity
        Give the worker Chalk credentials that identify the principal deploying it,
        so ``ChalkClient()`` inside a task authenticates without a client id/secret.
        ``ChalkIdentity(mode="sandbox")`` is not supported for scaling groups.
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
        chalk_identity=chalk_identity,
        chalk_client=chalk_client,
    ).deploy()
    return WorkflowWorker(scaling_group=scaling_group, task_queue=task_queue)


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
