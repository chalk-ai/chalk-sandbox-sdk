"""Describe a worker file's ``chalk.workflows`` definitions to the orchestrator.

After ``deploy_workflow_worker`` creates a scaling group revision, it registers the
workflows and tasks that revision serves: their names, source, and task settings. The
dashboard shows them next to the runs they produce. Registration is best effort; the
worker serves runs whether or not it succeeds.
"""

from __future__ import annotations

import inspect
import logging
import os
import types
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf.wkt import Duration

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.server.v1 import (
    workflow_orchestrator_connect as wo_connect,
)
from chalkcompute._gen.chalk.server.v1 import workflow_orchestrator_pb as wo_pb

logger = logging.getLogger(__name__)


def definition_specs(
    workflows: Iterable[Any],
    tasks: Iterable[Any],
    source: str,
) -> list[wo_pb.WorkflowDefinitionSpec]:
    """Specs for the workflows defined in ``source``, the tasks it defines, and the tasks
    those workflows name from other files.

    ``workflows`` and ``tasks`` are ``chalk.workflows`` definitions, such as the values of
    ``WORKFLOW_REGISTRY`` and ``TASK_REGISTRY``.
    """
    source = os.path.realpath(source)
    tasks = list(tasks)
    in_source = [w for w in workflows if _defined_in(w, source)]
    named: dict[str, list[str]] = {w.name: _task_names(w.fn, tasks) for w in in_source}
    referenced = {name for names in named.values() for name in names}
    specs = [_workflow_spec(w, named[w.name]) for w in in_source]
    specs.extend(
        _task_spec(t) for t in tasks if _defined_in(t, source) or t.name in referenced
    )
    return specs


def _defined_in(definition: Any, source: str) -> bool:
    return os.path.realpath(definition.filename) == source


def _task_names(fn: Any, tasks: list[Any]) -> list[str]:
    """Tasks the function's code names as module globals, including inside nested code
    such as comprehensions and lambdas, sorted by name. A task reached through another
    function is not found."""
    by_identity = {id(t): t for t in tasks}
    globals_ = getattr(fn, "__globals__", {})
    names: set[str] = set()
    pending = [fn.__code__]
    while pending:
        code = pending.pop()
        for name in code.co_names:
            task = by_identity.get(id(globals_.get(name)))
            if task is not None:
                names.add(task.name)
        pending.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
    return sorted(names)


def _source(definition: Any) -> wo_pb.WorkflowDefinitionSource:
    fn = inspect.unwrap(definition.fn)
    try:
        lines, first_line = inspect.getsourcelines(fn)
        code = "".join(lines)
    except (OSError, TypeError):
        code, first_line = "", 0
    return wo_pb.WorkflowDefinitionSource(
        # The local path is the deployer's, so only the file name is sent.
        file_name=os.path.basename(definition.filename),
        qualname=definition.qualname,
        first_line=first_line,
        code=code,
    )


def _description(definition: Any) -> str:
    return definition.description or inspect.getdoc(definition.fn) or ""


def _workflow_spec(
    workflow: Any, task_names: list[str]
) -> wo_pb.WorkflowDefinitionSpec:
    return wo_pb.WorkflowDefinitionSpec(
        kind=wo_pb.WorkflowDefinitionKind.WORKFLOW,
        name=workflow.name,
        description=_description(workflow),
        owner=workflow.owner or "",
        source=_source(workflow),
        task_names=task_names,
    )


def _task_spec(task: Any) -> wo_pb.WorkflowDefinitionSpec:
    return wo_pb.WorkflowDefinitionSpec(
        kind=wo_pb.WorkflowDefinitionKind.TASK,
        name=task.name,
        description=_description(task),
        source=_source(task),
        timeout=_duration(task.timeout),
        retries=task.retries,
        retry_delay=_duration(task.retry_delay),
    )


def _duration(value: timedelta | None) -> Duration | None:
    if value is None:
        return None
    micros = round(value.total_seconds() * 1_000_000)
    seconds, remainder = divmod(micros, 1_000_000)
    return Duration(seconds=seconds, nanos=remainder * 1_000)


def collect_definition_specs(source: str) -> list[wo_pb.WorkflowDefinitionSpec]:
    """Specs for ``source``'s definitions, importing it if this process has not.

    The deploying script is usually ``source`` itself, so its decorators have already
    registered. Otherwise the file is imported the way the worker imports it, without
    running its ``__main__`` block.
    """
    try:
        from chalk.workflows import TASK_REGISTRY, WORKFLOW_REGISTRY
    except ImportError:
        logger.debug("chalk.workflows is not installed; not registering definitions")
        return []
    real = os.path.realpath(source)
    registered = [*WORKFLOW_REGISTRY.values(), *TASK_REGISTRY.values()]
    if not any(_defined_in(d, real) for d in registered):
        try:
            # Added in a newer chalkpy than the one the SDK is checked against.
            from chalk.workflows.worker import (  # ty: ignore[unresolved-import]
                load_workflow_source,
            )
        except ImportError:
            logger.debug(
                "This chalkpy cannot import %s; not registering definitions", source
            )
            return []
        load_workflow_source(source)
    return definition_specs(WORKFLOW_REGISTRY.values(), TASK_REGISTRY.values(), source)


def register_workflow_definitions(
    scaling_group_name: str,
    revision_id: str,
    specs: list[wo_pb.WorkflowDefinitionSpec],
    *,
    chalk_client: Any | None = None,
    client: ConnectClient | None = None,
) -> bool:
    """Record ``specs`` as what ``revision_id`` of the group serves. Returns whether the
    orchestrator accepted them; a failure is logged, never raised."""
    connect = client if client is not None else ConnectClient(chalk_client=chalk_client)
    stub = connect.stub(wo_connect.WorkflowOrchestratorServiceClientSync)
    try:
        connect.call(
            stub.register_workflow_definitions,
            wo_pb.RegisterWorkflowDefinitionsRequest(
                scaling_group_name=scaling_group_name,
                scaling_group_revision_id=revision_id,
                definitions=specs,
            ),
            timeout=30,
        )
    except ConnectRpcError as exc:
        logger.warning(
            "Could not register the workflow definitions of %s; the worker still serves them: %s",
            scaling_group_name,
            exc,
        )
        return False
    return True
