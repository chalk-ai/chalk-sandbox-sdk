"""Bounded infrastructure retries for explicitly safe integration-test operations."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from connectrpc.code import Code
from connectrpc.errors import ConnectError

from chalkcompute import Container, ContainerError
from chalkcompute._container import ExecResult

logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_MAX_ATTEMPTS = 3


def retry_safe_operation(
    call: Callable[[], _T], *, retry_response: Callable[[_T], bool]
) -> _T:
    """Retry an idempotent test operation on known infrastructure failures.

    Callers must opt in only when repeating their operation is safe. In
    particular, this must not wrap arbitrary shell commands or assertions.
    Exhausted retries preserve the final response/exception for the test to fail.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            result = call()
        except ContainerError as exc:
            cause = exc.__cause__
            if (
                not isinstance(cause, ConnectError)
                or cause.code not in (Code.UNAVAILABLE, Code.DEADLINE_EXCEEDED)
                or attempt == _MAX_ATTEMPTS
            ):
                raise
        else:
            if not retry_response(result) or attempt == _MAX_ATTEMPTS:
                return result
        logger.warning(
            "Transient infrastructure failure in safe test operation; retrying (%d/%d)",
            attempt,
            _MAX_ATTEMPTS,
        )
        time.sleep(float(attempt))
    raise AssertionError("unreachable")


def exec_read_only(
    container: Container, *command: str, timeout_secs: int | None = None
) -> ExecResult:
    """Run a read-only fixture probe, tolerating transient kubelet startup errors."""
    return retry_safe_operation(
        lambda: container.exec(*command, timeout_secs=timeout_secs),
        retry_response=lambda result: (
            result.exit_code != 0
            and not result.stdout
            and result.stderr_text.startswith("exec startup failed:")
            and "remote error: tls: internal error" in result.stderr_text
        ),
    )
