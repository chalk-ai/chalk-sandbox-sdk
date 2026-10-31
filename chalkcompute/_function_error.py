"""Remote execution errors and presentation, independent of call transport."""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any


class FunctionError(Exception):
    """Base exception for Function errors."""


class RemoteExecutionError(FunctionError):
    """A remote invocation failed. The original exception is carried as data.

    ``remote_traceback`` contains user frames and exception chains;
    ``internal_traceback`` retains runtime diagnostics for debugging.
    """

    def __init__(
        self,
        function_name: str,
        exception_type: str,
        message: str,
        remote_traceback: str,
        internal_traceback: str,
        *,
        call_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.function_name = function_name
        self.exception_type = exception_type
        self.message = message
        self.remote_traceback = remote_traceback
        self.internal_traceback = internal_traceback
        self.call_id = call_id
        self.trace_id = trace_id
        super().__init__(f"{function_name}: {exception_type}: {message}")

    def __str__(self) -> str:
        return "\n".join(self._remote_lines())

    def __reduce__(self) -> Any:
        return (
            type(self),
            (
                self.function_name,
                self.exception_type,
                self.message,
                self.remote_traceback,
                self.internal_traceback,
            ),
            self.__dict__,
        )

    def _remote_lines(self) -> list[str]:
        if (
            self.exception_type == "ProxyError"
            and not self.remote_traceback
            and not self.trace_id
        ):
            suffix = f" (call {self.call_id})" if self.call_id else ""
            return [f"ProxyError: {self.message}{suffix}"]
        lines = [f"Remote execution failed: {self.function_name}"]
        if self.call_id:
            lines.append(f"Call ID: {self.call_id}")
        if self.trace_id:
            lines.append(f"Trace ID: {self.trace_id}")
        lines.extend(["", "Remote traceback (most recent call last):"])
        remote = self.remote_traceback.removeprefix(
            "Traceback (most recent call last):\n"
        )
        lines.extend(
            (remote or f"{self.exception_type}: {self.message}").rstrip().splitlines()
        )
        return lines

    def _render_traceback_(self) -> list[str]:
        # Only this exception opts into IPython rendering. Keep the actual
        # traceback intact for debuggers and exception capture integrations.
        package = str(Path(__file__).parent) + "/"
        local = [
            frame
            for frame in traceback.extract_tb(self.__traceback__)
            if not frame.filename.startswith(package)
            and "/IPython/core/" not in frame.filename
        ]
        lines = ["Traceback (most recent call last):"]
        ipython = sys.modules.get("IPython")
        shell = ipython.get_ipython() if ipython is not None else None
        filename_map = getattr(getattr(shell, "compile", None), "_filename_map", {})
        for frame in local:
            cell = filename_map.get(frame.filename)
            if cell is None:
                lines.extend(
                    "".join(traceback.format_list([frame])).rstrip().splitlines()
                )
            else:
                # Retain IPython's cell marker so Chalk can subtract its prelude
                # and substitute the displayed notebook cell name.
                lines.append(f"Cell In[{cell}], line {frame.lineno}")
                if frame.line:
                    lines.append(f"----> {frame.lineno} {frame.line}")
        lines.extend(["", *self._remote_lines()])
        return lines


def decode_remote_error(
    payload: Any, function_name: str, *, call_id: str | None = None
) -> RemoteExecutionError | None:
    """Decode the versioned runtime payload or a queue envelope around it.

    Never classify a free-text error by words in its message. Older runtimes
    and unknown payload versions retain their existing error behavior.
    """
    for _ in range(3):
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                return None
        if not isinstance(payload, dict):
            return None
        if payload.get("chalk_remote_error") == 1:
            exception = payload.get("exception")
            if not isinstance(exception, dict):
                return None
            names = ("kind", "message", "stacktrace", "internal_stacktrace")
            if not all(isinstance(exception.get(name), str) for name in names):
                return None
            return RemoteExecutionError(
                function_name,
                *(exception[name] for name in names),
                call_id=call_id,
                trace_id=payload.get("trace_id")
                if isinstance(payload.get("trace_id"), str)
                else None,
            )
        if "chalk_remote_error" in payload:
            return None
        if not isinstance(payload.get("exc_type"), str):
            return None
        payload = payload.get("message")
    return None


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    return value if isinstance(value, dict) else None


def _is_legacy_error(payload: dict[str, Any]) -> bool:
    return isinstance(payload.get("exc_type"), str) and isinstance(
        payload.get("message"), str
    )


def decode_deferred_failure(
    payload: Any, function_name: str, *, call_id: str | None = None
) -> RemoteExecutionError | None:
    """Decode a failure reported for a deferred call, including proxy failures.

    Only deferred result collection uses this. Transport errors keep
    :func:`decode_remote_error`, so a proxy envelope on a ``.remote()`` RPC
    error keeps its original exception and stale-connection handling.
    """
    error = decode_remote_error(payload, function_name, call_id=call_id)
    if error is not None:
        return error
    envelope = _json_object(payload)
    for _ in range(3):
        if envelope is None or not _is_legacy_error(envelope):
            return None
        inner = _json_object(envelope["message"])
        if envelope["exc_type"] == "ProxyError":
            # A proxy may wrap a legacy runtime error; report that error
            # rather than its JSON text.
            source = (
                inner if inner is not None and _is_legacy_error(inner) else envelope
            )
            traceback_text = source.get("traceback")
            return RemoteExecutionError(
                function_name,
                source["exc_type"],
                source["message"],
                traceback_text if isinstance(traceback_text, str) else "",
                "",
                call_id=call_id,
            )
        envelope = inner
    return None


def remote_error_from_exception(
    error: Exception, function_name: str
) -> RemoteExecutionError | None:
    from chalkcompute._remote_call_client import RemoteCallStatusError
    from connectrpc.errors import ConnectError

    if isinstance(error, RemoteCallStatusError) and len(error.args) == 2:
        return decode_remote_error(error.args[1], function_name)
    if isinstance(error, ConnectError):
        return decode_remote_error(error.message, function_name)
    return None
