"""Run ``chalk.workflows`` workflows with chalkcompute's ambient credentials.

The ``@workflow`` / ``@task`` decorators and the Temporal backend live in
chalkpy (``chalk.workflows``). These wrappers let a script written against
chalkcompute run them without constructing a ``ChalkClient`` by hand.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from chalkcompute._connect import chalk_client_from_env

if TYPE_CHECKING:
    from chalk.workflows import WorkflowDefinition
    from chalk.workflows._remote import WorkflowRunHandle


class WorkflowError(Exception):
    """Raised when a workflow cannot be started."""


def _client(environment: str | None) -> Any:
    try:
        import temporalio  # noqa: F401
    except ImportError as exc:
        raise WorkflowError(
            "Running workflows requires chalkpy's workflow extra; install "
            + "'chalkcompute[workflows]' or 'chalkpy[workflows]'"
        ) from exc
    return chalk_client_from_env(environment=environment)


def trigger_workflow(
    workflow: WorkflowDefinition[Any, Any] | str,
    input: Mapping[str, Any] | None = None,
    *,
    workflow_id: str | None = None,
    environment: str | None = None,
    wait: bool = False,
) -> WorkflowRunHandle | Any:
    """Start a deployed workflow on the environment's workflow workers.

    Parameters
    ----------
    workflow
        A ``@workflow``-decorated function, or the name of a deployed workflow.
    input
        Keyword arguments for the workflow function. Values must be
        JSON-serializable.
    workflow_id
        Idempotency key identifying this run. Defaults to a generated id.
    environment
        The environment to run against. Defaults to the ambient environment.
    wait
        If True, block until the workflow completes and return its result.
        Otherwise return a handle to the running workflow.
    """
    return _client(environment).trigger_workflow(
        workflow, input, workflow_id=workflow_id, wait=wait
    )
