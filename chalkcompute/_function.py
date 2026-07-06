"""
Deploy Python functions as versioned remote functions backed by ExternalFunctionCatalogService.

Usage:
    import chalkcompute

    @chalkcompute.function
    def add(x: int, y: int) -> int:
        return x + y

    local = add(1, 2)          # runs the Python body in-process
    remote = add.remote(1, 2)  # wire call to the deployed function

Or imperatively:

    from chalkcompute import RemoteFunction

    def add(x: int, y: int) -> int:
        return x + y

    f = RemoteFunction(add)
    f.deploy().wait_ready()
    print(f.remote(1, 2))
    f.delete()

"""

from __future__ import annotations

import ast
import asyncio
import contextvars
import datetime as _dt
import functools
import inspect
import json
import linecache
import logging
import os
import tempfile
import textwrap
import threading
import time
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Protocol,
    TypeVar,
    overload,
    runtime_checkable,
)

from chalkcompute._concurrency import ConcurrencyPolicy
from chalkcompute._connect import (
    ConnectClient,
    build_image,
    grpc_target_from_url,
    service_auth_context_from_env,
)
from chalkcompute import _local_dev
from chalkcompute._naming import sanitize_dns_label, validate_dns_label, validate_env
from chalkcompute._quota import QueuePolicy
from chalkcompute._rate_limit import RateLimitPolicy
from chalkcompute._remote_function_tracing import (
    TracingPolicy,
    TracingPolicyConfig,
    normalize_remote_function_tracing,
)
from chalkcompute._retry import RetryPolicy
from chalkcompute._tracing import (
    _instrumented,
    current_trace_metadata,
    remote_function_call_span,
)
from chalkcompute._volume import (
    VolumeDriver,
    VolumeError,
    VolumeMount,
    resolve_referenced_volume_type,
)
from chalkcompute._vvolume import VersionedVolumeClient

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from chalkcompute._image import Image as _Image
    from chalkcompute._secret import Secret as _Secret

SerializationFormat = typing.Literal["pyarrow"]
F = TypeVar("F", bound=Callable[..., Any])
_RemoteCallResult = TypeVar("_RemoteCallResult")
KnowledgeCutoff = str | _dt.datetime

_REMOTE_ENV_VAR = "_CHALK_REMOTE_FUNCTION"
_REQUEST_TIMESTAMP_HEADER = "x-chalk-request-timestamp"
_NO_ARGS_SENTINEL_COLUMN = "__chalk_no_args__"
_LOCAL_COPY_ENV_VAR = _local_dev._LOCAL_COPY_ENV_VAR
_LOCAL_WHEEL_ENV_VAR = _local_dev._LOCAL_WHEEL_ENV_VAR
_LOCAL_REMOTE_CALL_WHEEL_ENV_VAR = _local_dev._LOCAL_REMOTE_CALL_WHEEL_ENV_VAR
_STRIP_SOURCE_ENV_VAR = _local_dev._STRIP_SOURCE_ENV_VAR
# When enabled, route __call__ through the API server's CallExternalFunction
# RPC instead of connecting directly to the scaling group's web URL.
_CALL_VIA_API_ENV_VAR = "CHALK_FUNCTION_CALL_VIA_API"
_CATALOG_SVC = "chalk.externalfunctioncatalog.v1.ExternalFunctionCatalogService"
_SG_SVC = "chalk.scalinggroup.v1.ScalingGroupManagerService"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FunctionError(Exception):
    """Base exception for Function errors."""


class FunctionBuildError(FunctionError):
    """Raised when the function image build fails."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FunctionVersionInfo:
    """Metadata about a deployed function version."""

    id: str
    """Unique identifier of the function version."""

    function_name: str
    """Registered function name."""

    version: int
    """Monotonic version number for this function."""

    scaling_group_name: str
    """Name of the scaling group backing this version."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class DeployedFunction(Protocol):
    """Common interface for RemoteFunction and _NoOpFunction."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...
    def remote(self, *args: Any, **kwargs: Any) -> Any: ...
    def defer(self, *args: Any, **kwargs: Any) -> FunctionCallHandle: ...
    def deploy(self, **kwargs: Any) -> DeployedFunction: ...
    def wait_ready(self, **kwargs: Any) -> DeployedFunction: ...
    def delete(self) -> None: ...
    def refresh(self) -> Any: ...


# ---------------------------------------------------------------------------
# Source transformation helpers
# ---------------------------------------------------------------------------


def _strip_chalkcompute_from_source(source: str) -> str:
    """Remove chalkcompute imports, decorators, and lifecycle calls.

    Preserves all other code (helpers, constants, classes, non-chalkcompute
    imports) so the deployed module is self-contained minus the SDK.

    Parameters
    ----------
    source
        Python source text to rewrite.

    Returns
    -------
    str
        Rewritten source with chalkcompute references removed.
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)

    # Collect line ranges to remove (1-indexed, inclusive)
    remove_ranges: list[tuple[int, int]] = []

    # Track aliases that let users spell SDK decorators as either
    # @chalkcompute.function, @cc.function, @function, or @traced.
    chalkcompute_module_names = {"chalkcompute"}
    chalkcompute_decorator_names: set[str] = set()

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "chalkcompute":
                    chalkcompute_module_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("chalkcompute"):
                for alias in node.names:
                    if alias.name == "*":
                        chalkcompute_decorator_names.add("function")
                        chalkcompute_decorator_names.add("traced")
                    elif alias.name in {"function", "traced"}:
                        chalkcompute_decorator_names.add(alias.asname or alias.name)

    # Track names of functions decorated with a chalkcompute function decorator.
    decorated_names: set[str] = set()

    for node in ast.iter_child_nodes(tree):
        # Remove: import chalkcompute / from chalkcompute import ...
        if isinstance(node, ast.Import):
            if any(alias.name.startswith("chalkcompute") for alias in node.names):
                remove_ranges.append((node.lineno, node.end_lineno or node.lineno))
                continue
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("chalkcompute"):
                remove_ranges.append((node.lineno, node.end_lineno or node.lineno))
                continue

        # Remove @chalkcompute.function(...) / @chalkcompute.traced decorators
        # from function defs.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                dec_source = ast.get_source_segment(source, dec)
                if (
                    dec_source is not None and "chalkcompute" in dec_source
                ) or _is_chalkcompute_decorator(
                    dec,
                    module_names=chalkcompute_module_names,
                    decorator_names=chalkcompute_decorator_names,
                    attribute_names={"function", "traced"},
                ):
                    remove_ranges.append((dec.lineno, dec.end_lineno or dec.lineno))
                    decorated_names.add(node.name)

    # Remove method calls on decorated functions (e.g. add.wait_ready())
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if (
                isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id in decorated_names
            ):
                remove_ranges.append((node.lineno, node.end_lineno or node.lineno))

    # Build output, skipping removed line ranges
    removed_lines: set[int] = set()
    for start, end in remove_ranges:
        for ln in range(start, end + 1):
            removed_lines.add(ln)

    kept = [line for i, line in enumerate(lines, start=1) if i not in removed_lines]
    return "".join(kept)


def _is_chalkcompute_decorator(
    decorator: ast.expr,
    *,
    module_names: set[str],
    decorator_names: set[str],
    attribute_names: set[str],
) -> bool:
    """Return whether an AST decorator refers to a chalkcompute decorator."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator

    if isinstance(target, ast.Name):
        return target.id in decorator_names

    if isinstance(target, ast.Attribute):
        return (
            target.attr in attribute_names
            and isinstance(target.value, ast.Name)
            and target.value.id in module_names
        )

    return False


def _generate_handler_shim(
    func_name: str,
    *,
    batched: bool = False,
    bind_models: bool = False,
) -> str:
    """Generate a ``handler.py`` that bridges Arrow columnar data to scalar calls.

    Non-batched mode: ``chalk-remote-call`` passes ``event`` as
    ``dict[str, pa.Array]`` and calls ``handler(event, context)``. Generator
    outputs stream as individual response chunks.

    Batched mode: the server coalesces multiple callers and passes
    ``events: list[dict[str, pa.Array]]`` / ``contexts: list[dict]`` to
    ``handler(events, contexts)``. The shim returns a list-of-lists —
    one inner list per caller. Generators are not supported in this mode.

    Parameters
    ----------
    func_name
        Name of the user function to import and invoke.
    batched
        Whether the deployed function enables batching.
    bind_models
        When ``True``, the shim calls ``apply_models_from_env`` on import to
        bind every model declared via the decorator's ``models=`` kwarg before
        the user module is imported. Required when ``models=`` is set so that
        any module-level ``F.catalog_call("model.<name>", ...)`` references can
        resolve.

    Returns
    -------
    str
        Python source for the handler shim.
    """
    bootstrap = (
        "from chalkcompute._model import apply_models_from_env as _apply_models\n"
        "_apply_models()\n"
        "\n"
        if bind_models
        else ""
    )
    if batched:
        # Flatten all (caller_idx, row_idx, kwargs) across every caller so
        # each item is an independent unit of work.  Results are collected in
        # completion order (not submission order), then regrouped per caller.
        # This means a slow item in one caller never blocks another caller's
        # items from completing — true promise semantics.
        return bootstrap + textwrap.dedent(f"""\
            import inspect
            from concurrent.futures import ThreadPoolExecutor, as_completed

            from user_module import {func_name}

            _NO_ARGS_SENTINEL_COLUMN = {_NO_ARGS_SENTINEL_COLUMN!r}
            _target = inspect.unwrap({func_name})
            _is_coroutine = inspect.iscoroutinefunction(_target)


            def _flatten_events(events):
                work = []
                for ci, event in enumerate(events):
                    columns = {{
                        k: v.to_pylist()
                        for k, v in event.items()
                        if k != _NO_ARGS_SENTINEL_COLUMN
                    }}
                    n = len(next(iter(columns.values()))) if columns else 1
                    for ri in range(n):
                        work.append((ci, ri, {{k: v[ri] for k, v in columns.items()}}))
                return work


            def _regroup(work, flat_results, n_callers):
                bucket = {{}}
                for (ci, ri, _), val in zip(work, flat_results):
                    bucket.setdefault(ci, {{}})[ri] = val
                return [[bucket[ci][ri] for ri in sorted(bucket[ci])]
                        for ci in range(n_callers)]


            def handler(events, contexts):
                work = _flatten_events(events)
                if _is_coroutine:
                    import asyncio
                    async def _run_all():
                        coros = [{func_name}(**kw) for _, _, kw in work]
                        flat = await asyncio.gather(*coros)
                        return _regroup(work, flat, len(events))
                    return asyncio.run(_run_all())
                bucket = {{}}
                with ThreadPoolExecutor(max_workers=max(1, len(work))) as ex:
                    futures = {{
                        ex.submit({func_name}, **kw): (ci, ri)
                        for ci, ri, kw in work
                    }}
                    for fut in as_completed(futures):
                        ci, ri = futures[fut]
                        bucket.setdefault(ci, {{}})[ri] = fut.result()
                return [[bucket[ci][ri] for ri in sorted(bucket[ci])]
                        for ci in range(len(events))]
            """)
    return bootstrap + textwrap.dedent(f"""\
        import inspect

        from user_module import {func_name}

        _NO_ARGS_SENTINEL_COLUMN = {_NO_ARGS_SENTINEL_COLUMN!r}
        _target = inspect.unwrap({func_name})
        _is_sync_gen = inspect.isgeneratorfunction(_target)
        _is_async_gen = inspect.isasyncgenfunction(_target)
        _is_coroutine = inspect.iscoroutinefunction(_target) and not _is_async_gen


        def handler(event, context):
            event = {{
                k: v
                for k, v in event.items()
                if k != _NO_ARGS_SENTINEL_COLUMN
            }}
            n = len(next(iter(event.values()))) if event else 1
            if _is_sync_gen:
                def _gen():
                    for i in range(n):
                        kwargs = {{k: v[i].as_py() for k, v in event.items()}}
                        yield from {func_name}(**kwargs)
                return _gen()
            elif _is_async_gen:
                async def _agen():
                    for i in range(n):
                        kwargs = {{k: v[i].as_py() for k, v in event.items()}}
                        async for item in {func_name}(**kwargs):
                            yield item
                return _agen()
            elif _is_coroutine:
                import asyncio
                results = []
                for i in range(n):
                    kwargs = {{k: v[i].as_py() for k, v in event.items()}}
                    results.append(asyncio.run({func_name}(**kwargs)))
                return results
            else:
                results = []
                for i in range(n):
                    kwargs = {{k: v[i].as_py() for k, v in event.items()}}
                    results.append({func_name}(**kwargs))
                return results
        """)


def _write_temp(content: str) -> str:
    """Write content to a temp file and return its path."""
    f = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".py",
        prefix="chalkfn_",
        delete=False,
    )
    f.write(content)
    f.close()
    return f.name


def _should_strip_source() -> bool:
    return _local_dev.should_strip_source()


def _should_call_via_api() -> bool:
    return os.environ.get(_CALL_VIA_API_ENV_VAR, "") == "1"


def _has_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _format_knowledge_cutoff(value: KnowledgeCutoff) -> str:
    if isinstance(value, str):
        if not value:
            raise ValueError("knowledge cutoff must be a non-empty string")
        return value

    if not isinstance(value, _dt.datetime):
        raise TypeError(
            "knowledge cutoff must be a datetime.datetime or ISO timestamp string"
        )

    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    else:
        value = value.astimezone(_dt.timezone.utc)
    return value.isoformat()


async def _run_in_worker_thread(
    func: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    context = contextvars.copy_context()

    def complete_with_result(value: Any) -> None:
        if not future.cancelled():
            future.set_result(value)

    def complete_with_exception(exc: BaseException) -> None:
        if not future.cancelled():
            future.set_exception(exc)

    def run() -> None:
        try:
            value = context.run(func, *args, **kwargs)
        except BaseException as exc:
            loop.call_soon_threadsafe(complete_with_exception, exc)
        else:
            loop.call_soon_threadsafe(complete_with_result, value)

    threading.Thread(target=run, daemon=True).start()
    return await future


def _remote_call_error_message(error: BaseException) -> str:
    """Return a best-effort transport error string without assuming error type."""
    details = None
    details_fn = getattr(error, "details", None)
    if callable(details_fn):
        try:
            details = details_fn()
        except Exception:
            details = None
    message = str(error)
    if details:
        message = f"{message} {details}"
    return message


def _is_stale_remote_call_error_message(message: str | None) -> bool:
    if not message:
        return False
    lower = message.lower()
    if "transport error:" in lower:
        return True
    if "grpc status:" not in lower and "rpcerror" not in lower:
        return False
    return any(
        marker in lower
        for marker in (
            "unavailable",
            "fd shutdown",
            "goaway",
            "connection reset",
            "connection refused",
            "delayed connect error",
            "upstream connect error",
            "broken pipe",
            "connection closed",
        )
    )


def _is_retryable_stale_remote_call_error_message(message: str | None) -> bool:
    if not message:
        return False
    lower = message.lower()
    return "fd shutdown" in lower or "goaway" in lower


def _is_stale_remote_call_error(error: BaseException) -> bool:
    try:
        import grpc
    except ImportError:  # pragma: no cover - grpc is a runtime dependency
        return _is_stale_remote_call_error_message(_remote_call_error_message(error))

    if isinstance(error, grpc.RpcError):
        code_fn = getattr(error, "code", None)
        if callable(code_fn):
            try:
                if code_fn() == grpc.StatusCode.UNAVAILABLE:
                    return True
            except Exception:
                pass
        try:
            if grpc.StatusCode.UNAVAILABLE.name in _remote_call_error_message(error):
                return True
        except Exception:
            pass
    return _is_stale_remote_call_error_message(_remote_call_error_message(error))


def _is_retryable_stale_remote_call_error(error: BaseException) -> bool:
    return _is_retryable_stale_remote_call_error_message(
        _remote_call_error_message(error)
    )


def _should_retry_stale_remote_call(
    retry_stale: bool | Callable[[BaseException], bool],
    error: BaseException,
) -> bool:
    if callable(retry_stale):
        return retry_stale(error)
    return retry_stale


# Exception "kind" values that mark a transient, infra-level failure originating
# *outside* the user's handler — a dropped proxy/queue connection rather than an
# exception raised by the function body. Safe to retry by resubmitting the call.
#
# A failed call's ``error_message`` is a JSON-encoded ``chalk.common.v1.ChalkError``
# flattened to ``{"exc_type": <ChalkException.kind>, "message": ...}``. We classify
# on the discrete ``exc_type`` rather than substring-matching the free-text message,
# so a user error whose message merely mentions an infra term is not misclassified,
# and rewording the message can't silently flip retry behavior. The in-pod runtime
# wraps every user-handler failure as RuntimeError/ValueError/TypeError, so those
# kinds are (correctly) absent here and never retried.
#
# Confirmed from observed engine output: the function proxy reports
# ``{"exc_type": "ProxyError", "message": "CallFunction RPC failed"}`` when it
# cannot reach or stream from the function pod. Extend this set as additional
# infra kinds are confirmed against the engine's error taxonomy.
_RETRYABLE_EXC_TYPES = frozenset(
    {
        "ProxyError",
    }
)
# Total attempts (initial + retries) for a deferred call that fails transiently
# before any result is observed. Overridable per-handle for testing.
_STREAM_MAX_ATTEMPTS = 3
# Base delay for exponential backoff between resubmissions, in seconds.
_STREAM_RETRY_BASE_DELAY = 0.5


def _failed_call_exc_type(error_message: str | None) -> str | None:
    """Extract the structured exception kind from a failed call's error payload.

    The queue surfaces a failed call's error as a JSON string of the form
    ``{"exc_type": ..., "message": ...}`` — a flattened
    ``chalk.common.v1.ChalkError`` whose ``exception.kind`` becomes ``exc_type``.
    Returns ``None`` when the payload is missing or not that structured shape.
    """
    if not error_message:
        return None
    try:
        payload = json.loads(error_message)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    exc_type = payload.get("exc_type")
    return exc_type if isinstance(exc_type, str) else None


def _is_transient_call_error(error_message: str | None) -> bool:
    """Whether a failed call is a retryable infra blip, by its structured exc_type.

    Unknown or unstructured errors are treated as non-retryable, so retry stays
    conservative: we never resubmit a call we can't positively classify as infra.
    """
    return _failed_call_exc_type(error_message) in _RETRYABLE_EXC_TYPES


class FunctionCallHandle:
    """Handle returned by :meth:`RemoteFunction.defer`."""

    def __init__(
        self,
        function: RemoteFunction,
        call_id: str,
        *,
        request_bytes: bytes | None = None,
    ) -> None:
        self._function = function
        self._call_id = call_id
        self._func_name = function._func_name
        self._is_async = bool(getattr(function, "_is_async", False))
        self._cursor = ""
        self._poll_count = 0
        # The serialized request, retained so the call can be resubmitted after
        # a transient failure. ``None`` disables retry (the call cannot be
        # reconstructed, e.g. a handle built without its request payload).
        self._request_bytes = request_bytes
        self._max_attempts = _STREAM_MAX_ATTEMPTS

    @property
    def call_id(self) -> str:
        return self._call_id

    def poll(self) -> Any:
        result = self._function.poll(
            self._call_id,
            cursor=self._cursor,
            poll_count=self._poll_count,
        )
        self._cursor = result.cursor
        self._poll_count = result.poll_count
        return result

    def _can_retry(self, attempt: int, message: str | None) -> bool:
        """Whether a transient failure on ``attempt`` is worth resubmitting."""
        return (
            attempt < self._max_attempts
            and self._request_bytes is not None
            and _is_transient_call_error(message)
        )

    def _retry_delay(self, retry_index: int) -> float:
        """Exponential backoff (seconds) before the ``retry_index``-th retry."""
        return _STREAM_RETRY_BASE_DELAY * (2 ** (retry_index - 1))

    def _resubmit(self) -> None:
        """Re-enqueue the original request and reset poll state to the new call."""
        assert self._request_bytes is not None  # guarded by _can_retry
        new_call_id = self._function._enqueue(self._request_bytes)
        logger.warning(
            "Remote call %s failed transiently; resubmitted as %s",
            self._call_id,
            new_call_id,
        )
        self._call_id = new_call_id
        self._cursor = ""
        self._poll_count = 0

    def _timeout_error(self, last_status: str) -> TimeoutError:
        return TimeoutError(
            f"Remote call {self._call_id} ({self._func_name!r}) timed out "
            f"(last status: {last_status})"
        )

    def get(self, timeout: float | None = None) -> Any:
        if _has_running_event_loop():
            return self._async_get(timeout)
        return self._sync_get(timeout)

    def _sync_get(self, timeout: float | None = None) -> Any:
        deadline = None if timeout is None else time.monotonic() + timeout
        attempt = 1

        while True:
            # Each attempt re-collects from scratch; partial chunks from a failed
            # attempt are discarded, so resubmitting after partial output is safe
            # for get() (unlike stream(), which has already yielded to a caller).
            chunks: list[bytes] = []
            last_status = "unknown"
            while True:
                result = self.poll()
                last_status = result.status_name
                chunks.extend(result.chunks)

                if result.is_failed:
                    message = result.error_message or "Remote call failed"
                    if self._can_retry(attempt, message):
                        break
                    raise FunctionError(message)

                if result.is_completed:
                    return self._function._materialize_result_chunks(chunks)

                if deadline is not None and time.monotonic() >= deadline:
                    raise self._timeout_error(last_status)

            attempt += 1
            time.sleep(self._retry_delay(attempt - 1))
            self._resubmit()

    async def _async_get(self, timeout: float | None = None) -> Any:
        return await _run_in_worker_thread(self._sync_get, timeout)

    def stream(self, timeout: float | None = None) -> Any:
        if self._is_async:
            return self._async_stream(timeout)
        return self._sync_stream(timeout)

    def _sync_stream(self, timeout: float | None = None) -> Any:
        import pyarrow as pa

        deadline = None if timeout is None else time.monotonic() + timeout
        attempt = 1
        yielded_any = False

        while True:
            last_status = "unknown"
            while True:
                result = self.poll()
                last_status = result.status_name
                for chunk in result.chunks:
                    reader = pa.ipc.open_stream(pa.BufferReader(chunk))
                    for batch in reader:
                        yielded_any = True
                        yield batch

                if result.is_failed:
                    message = result.error_message or "Remote call failed"
                    # Once any batch has reached the caller the stream cannot be
                    # cleanly restarted (it would replay from the beginning), so
                    # only retry a failure that happened before the first yield.
                    if not yielded_any and self._can_retry(attempt, message):
                        break
                    raise FunctionError(message)

                if result.is_completed:
                    return

                if deadline is not None and time.monotonic() >= deadline:
                    raise self._timeout_error(last_status)

            attempt += 1
            time.sleep(self._retry_delay(attempt - 1))
            self._resubmit()

    async def _async_stream(self, timeout: float | None = None) -> Any:
        import pyarrow as pa

        deadline = None if timeout is None else time.monotonic() + timeout
        attempt = 1
        yielded_any = False

        while True:
            last_status = "unknown"
            while True:
                result = await asyncio.to_thread(self.poll)
                last_status = result.status_name
                for chunk in result.chunks:
                    reader = pa.ipc.open_stream(pa.BufferReader(chunk))
                    for batch in reader:
                        yielded_any = True
                        yield batch

                if result.is_failed:
                    message = result.error_message or "Remote call failed"
                    if not yielded_any and self._can_retry(attempt, message):
                        break
                    raise FunctionError(message)

                if result.is_completed:
                    return

                if deadline is not None and time.monotonic() >= deadline:
                    raise self._timeout_error(last_status)

            attempt += 1
            await asyncio.sleep(self._retry_delay(attempt - 1))
            await asyncio.to_thread(self._resubmit)


async def _gather_async(
    handles: list[FunctionCallHandle],
    *,
    timeout: float | None = None,
) -> list[Any]:
    return list(
        await asyncio.gather(*(handle.get(timeout=timeout) for handle in handles))
    )


def gather(
    handles: typing.Iterable[FunctionCallHandle],
    *,
    timeout: float | None = None,
) -> Any:
    """Wait for deferred function calls concurrently and return results in order.

    In synchronous code this blocks and returns ``list[Any]``. Inside a running
    event loop it returns an awaitable, matching :meth:`FunctionCallHandle.get`.
    """
    handle_list = list(handles)
    for i, handle in enumerate(handle_list):
        if not callable(getattr(handle, "get", None)):
            raise TypeError(
                f"gather() expects deferred-call handles from .defer(), but item "
                f"{i} is a {type(handle).__name__}. .remote() runs the call and "
                f"returns its result, not a handle — use .defer() to fan out, e.g. "
                f"gather(fn.defer(x) for x in xs)."
            )
    if _has_running_event_loop():
        return _gather_async(handle_list, timeout=timeout)
    return asyncio.run(_gather_async(handle_list, timeout=timeout))


def _source_text_for_deploy(source_file: str) -> str:
    try:
        return Path(source_file).read_text()
    except OSError as file_error:
        cached_lines = linecache.getlines(source_file)
        if cached_lines:
            return "".join(cached_lines)

        raise FunctionBuildError(
            f"Could not read source for {source_file!r} from disk or the Python source cache."
        ) from file_error


def _source_file_for_deploy(func: Callable[..., Any]) -> str:
    return inspect.getfile(inspect.unwrap(func))


def _chalkcompute_bound_names(source: str) -> set[str]:
    """Names that ``source`` binds to chalkcompute via import.

    Covers both ``import chalkcompute [as x]`` (binds the root module name) and
    ``from chalkcompute[.sub] import a, b as c`` (binds the imported symbols).
    """
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "chalkcompute" or alias.name.startswith(
                    "chalkcompute."
                ):
                    names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "chalkcompute" or module.startswith("chalkcompute."):
                for alias in node.names:
                    if alias.name != "*":  # can't enumerate names from a star import
                        names.add(alias.asname or alias.name)
    return names


def _loaded_names_in_bodies(entrypoint_sources: typing.Iterable[str]) -> set[str]:
    """Bare names read (``Load`` context) inside the *bodies* of the given defs.

    Each item in ``entrypoint_sources`` is the source of a single deployed
    function/method (from :func:`inspect.getsource`). Only statements inside the
    function body are inspected — the decorator list (which always references
    chalkcompute) is skipped, and unrelated functions sharing the same module
    are never considered.
    """
    used: set[str] = set()
    for src in entrypoint_sources:
        try:
            tree = ast.parse(textwrap.dedent(src))
        except (SyntaxError, IndentationError):
            continue
        for top in tree.body:
            if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for stmt in top.body:  # body only — skip decorators
                    for node in ast.walk(stmt):
                        if isinstance(node, ast.Name) and isinstance(
                            node.ctx, ast.Load
                        ):
                            used.add(node.id)
    return used


def _assert_stripped_source_self_contained(
    original_source: str,
    stripped_source: str,
    *,
    label: str,
    entrypoint_sources: typing.Sequence[str],
) -> None:
    """Fail fast if stripping chalkcompute left dangling references in the body.

    In strip mode the SDK is not installed in the deploy image and chalkcompute
    imports/decorators are removed from the source. If a deployed entrypoint's
    body still *uses* a name that a now-removed chalkcompute import had bound,
    and nothing rebinds it (e.g. an inner ``import chalkcompute`` that survives
    stripping), the deployed module raises ``NameError`` at call time. Surface
    that here, at deploy, with an actionable message instead of an opaque
    container-side error.

    Only the bodies in ``entrypoint_sources`` (the deployed function / class
    methods) are inspected; unrelated functions in the same file are ignored.
    """
    if not entrypoint_sources:
        return
    bound_before = _chalkcompute_bound_names(original_source)
    bound_after = _chalkcompute_bound_names(stripped_source)
    used = _loaded_names_in_bodies(entrypoint_sources)
    dangling = sorted((bound_before - bound_after) & used)
    if not dangling:
        return
    names = ", ".join(repr(n) for n in dangling)
    raise FunctionBuildError(
        f"{label} references chalkcompute ({names}) but is being deployed in "
        f"strip mode (CHALK_FUNCTION_STRIP_SOURCE, or a 0.0.0 source checkout), "
        f"which removes the chalkcompute SDK from the image and strips its "
        f"imports from the source — the deployed code would fail with NameError "
        f"at call time.\n"
        f"To make nested chalkcompute calls work, deploy from an environment "
        f"with a published chalkcompute installed (so strip mode is off and the "
        f"SDK is installed and importable), or set CHALK_FUNCTION_LOCAL_SDK_COPY=1 "
        f"with CHALK_FUNCTION_STRIP_SOURCE=0 to mount the local SDK. For a "
        f"@chalkcompute.function you may instead install chalkcompute in the "
        f"image and import it inside the function body (an inner import survives "
        f"stripping):\n"
        f'    @chalkcompute.function(image=Image.debian_slim().pip_install(["chalkcompute>=2.1.0"]))\n'
        f"    def my_fn(...):\n"
        f"        import chalkcompute\n"
        f"        ..."
    )


def _entrypoint_source(func: Callable[..., Any]) -> list[str]:
    """Best-effort source of a single deployed callable, for the strip guard.

    Returns ``[source]`` or ``[]`` when the source can't be recovered (e.g. a
    dynamically-defined function), in which case the guard is simply skipped.
    """
    try:
        return [inspect.getsource(inspect.unwrap(func))]
    except (OSError, TypeError):
        return []


def _prepare_source_file(
    source_file: str,
    *,
    label: str | None = None,
    entrypoint_sources: typing.Sequence[str] | None = None,
) -> tuple[str, bool]:
    """Return the source file path to deploy, optionally stripping chalkcompute.

    Parameters
    ----------
    source_file
        Path to the original user source file.
    label
        Human-readable identifier (e.g. function name) used in the strip-mode
        guard error; defaults to the source file path.
    entrypoint_sources
        Source of the deployed function(s) whose bodies the strip-mode guard
        inspects for dangling chalkcompute references. When empty/``None`` the
        guard is skipped (caller supplied nothing to scope to).

    Returns
    -------
    tuple of (str, bool)
        ``(path, is_temp)`` — if stripping produced a temp file, the caller
        should clean it up.
    """
    source_path = Path(source_file)
    if not _should_strip_source():
        if source_path.is_file():
            return source_file, False
        return _write_temp(_source_text_for_deploy(source_file)), True

    source = _source_text_for_deploy(source_file)
    stripped = _strip_chalkcompute_from_source(source)
    _assert_stripped_source_self_contained(
        source,
        stripped,
        label=label or f"Function source {source_file!r}",
        entrypoint_sources=list(entrypoint_sources or []),
    )
    return _write_temp(stripped), True


# ---------------------------------------------------------------------------
# Shared image / deploy helpers (used by both function.py and cls.py)
# ---------------------------------------------------------------------------


def _local_install_entrypoint(handler_cmd: list[str]) -> list[str]:
    """Wrap ``handler_cmd`` with a local SDK install when requested.

    Parameters
    ----------
    handler_cmd
        Original handler command to wrap.

    Returns
    -------
    list of str
        Possibly-wrapped entrypoint command.
    """
    return _local_dev.wrap_entrypoint_for_local_dev(handler_cmd)


_REMOTE_CALL_RUNTIME_PACKAGE = "chalk-remote-call-python"


def _remote_call_runtime_package_with_extras(extras: list[str]) -> str:
    if not extras:
        package = _REMOTE_CALL_RUNTIME_PACKAGE
    else:
        package = f"{_REMOTE_CALL_RUNTIME_PACKAGE}[{','.join(sorted(extras))}]"

    try:
        from importlib.metadata import PackageNotFoundError, version as _pkg_version

        return f"{package}=={_pkg_version(_REMOTE_CALL_RUNTIME_PACKAGE)}"
    except PackageNotFoundError:
        return package


def _make_base_image(
    user_image: _Image | None,
    *,
    remote_call_runtime_package: str = _REMOTE_CALL_RUNTIME_PACKAGE,
) -> _Image:
    """Create the base deploy image with SDK packages and optional local overlay."""
    from chalkcompute._image import Image

    base = user_image if user_image is not None else Image.debian_slim()
    return typing.cast(
        "_Image",
        _local_dev.apply_local_dev_mounts(
            base,
            remote_call_runtime_package=remote_call_runtime_package,
        ),
    )


def _preserve_functionlike_metadata(wrapper: Any, func: Callable) -> None:
    """Expose enough function metadata for inspect's function-kind checks."""
    code = getattr(func, "__code__", None)
    if code is None:
        return
    setattr(wrapper, "__code__", code)
    setattr(wrapper, "__defaults__", getattr(func, "__defaults__", None))
    setattr(wrapper, "__kwdefaults__", getattr(func, "__kwdefaults__", None))


def _build_and_upload(
    client: ConnectClient,
    deploy_image: _Image,
    *,
    label: str,
    volume_prefix: str,
    poll_interval: float,
    build_timeout: float,
    progress_parent: str | None = None,
) -> tuple[str, list[VolumeMount], VolumeDriver | None]:
    """Build the container image and upload lazy local files.

    Parameters
    ----------
    client
        Connect RPC client.
    deploy_image
        Declarative image to build.
    label
        Human-readable label used in progress output.
    volume_prefix
        Prefix for any new volumes created for lazy local files.
    poll_interval
        Seconds between build status polls.
    build_timeout
        Maximum seconds to wait for the build to finish.
    progress_parent
        Optional parent key for nested progress reporting.

    Returns
    -------
    tuple of (str, list of VolumeMount, VolumeDriver or None)
        ``(image_uri, new_volumes, driver)``. The driver is returned only so
        the caller can later delete the uploaded volumes; mount types live on
        the returned :class:`VolumeMount` objects, never on the driver.
    """
    from chalkcompute._progress import progress_add, progress_done

    ikey = f"image:{label}"
    progress_add(ikey, "image", "Building image", parent=progress_parent)

    logger.info("Building image for %s...", label)
    image_uri = build_image(
        client, deploy_image, poll_interval=poll_interval, timeout=build_timeout
    )
    logger.info("Image built: %s", image_uri)
    progress_done(ikey)

    new_volumes: list[VolumeMount] = []
    driver: VolumeDriver | None = None
    if deploy_image.lazy_local_files:
        vkey = f"upload:{label}"
        progress_add(
            vkey,
            "volume",
            f"Uploading {len(deploy_image.lazy_local_files)} file(s)",
            parent=progress_parent,
        )
        logger.info("Uploading %d local file(s)...", len(deploy_image.lazy_local_files))
        driver = VersionedVolumeClient.from_connect(client)
        new_volumes = driver.upload_lazy_files(
            deploy_image,
            volume_prefix=volume_prefix,
        )
        progress_done(vkey)
    return image_uri, new_volumes, driver


def _cleanup_temp_files(paths: list[str]) -> None:
    """Remove temporary files, ignoring errors.

    Parameters
    ----------
    paths
        Paths to temporary files to remove.
    """
    for tmp in paths:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _make_container_spec(
    *,
    name: str,
    image_uri: str,
    entrypoint: list[str],
    env: dict[str, str],
    cpu: str | None,
    memory: str | None,
    gpu: str | None,
    volumes: list[VolumeMount],
) -> dict[str, Any]:
    """Build a container spec dict for ``CreateExternalFunctionVersion``.

    Parameters
    ----------
    name
        Container/scaling-group name.
    image_uri
        Fully-qualified image URI.
    entrypoint
        Entrypoint command to run in the container.
    env
        Additional environment variables beyond the SDK defaults.
    cpu
        Optional CPU resource request.
    memory
        Optional memory resource request.
    gpu
        Optional GPU resource request.
    volumes
        List of :class:`VolumeMount`. Each mount carries its own ``type``,
        which is the value stamped into the request; the type is never
        recomputed from a driver here.

    Returns
    -------
    dict
        Container spec dictionary.

    Raises
    ------
    VolumeError
        If a volume is missing its mount type. After typed volumes flow from
        upload/normalization the type is always known, so this guards against
        an unexpected untyped volume rather than silently guessing one.
    """
    spec: dict[str, Any] = {
        "name": name,
        "image": image_uri,
        "port": 6666,
        "protocol": "grpc",
        "entrypoint": entrypoint,
        "envVars": {
            "PYTHONPATH": "/app",
            _REMOTE_ENV_VAR: "1",
            **env,
        },
    }
    resources: dict[str, str] = {}
    if cpu:
        resources["cpu"] = cpu
    if memory:
        resources["memory"] = memory
    if gpu:
        resources["gpu"] = gpu
    if resources:
        spec["resources"] = resources
    if volumes:
        for v in volumes:
            if not v.type:
                raise VolumeError(
                    f"Volume {v.name!r} mounted at {v.mount_path!r} has no mount "
                    "type; refusing to guess one for the container spec"
                )
        spec["volumes"] = [v.to_spec_dict() for v in volumes]
    return spec


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def _build_schemas(func: Callable[..., Any]) -> tuple[Any, Any]:
    """Derive input/output ``arrow_pb2.Schema`` protos from function type hints.

    Goes directly from Python type annotations to ``arrow_pb2.Schema`` protos
    without an intermediate PyArrow representation.

    Also stores function traits (``is_generator``, ``is_async``) in the output
    schema's metadata so the client can discover them from the catalog.

    Parameters
    ----------
    func
        User function to inspect.

    Returns
    -------
    tuple
        ``(input_schema, output_schema)`` Arrow schema protos.

    Raises
    ------
    TypeError
        If any parameter or the return type lacks an annotation.
    """
    import collections.abc as _cabc

    from chalkcompute._arrow_schema import rich_to_arrow_schema

    hints = typing.get_type_hints(func)
    sig = inspect.signature(func)

    # Input: one Field per parameter
    input_types: dict[str, type] = {}
    for param_name in sig.parameters:
        param_type = hints.get(param_name)
        if param_type is None:
            raise TypeError(f"Parameter '{param_name}' must have a type annotation")
        input_types[param_name] = param_type
    input_schema = rich_to_arrow_schema(input_types)

    # Output: single column "result"
    # For generator functions (Iterator[T], Generator[T, ...]), unwrap to the yield type.
    return_type = hints.get("return")
    if return_type is None:
        raise TypeError("Function must have a return type annotation")

    is_generator = False
    is_async = inspect.iscoroutinefunction(func) or inspect.isasyncgenfunction(func)

    origin = getattr(return_type, "__origin__", None)
    if origin is not None:
        if origin in (_cabc.Iterator, _cabc.Generator):
            is_generator = True
            args = getattr(return_type, "__args__", ())
            if args:
                return_type = args[0]
        elif origin in (_cabc.AsyncIterator, _cabc.AsyncGenerator):
            is_generator = True
            is_async = True
            args = getattr(return_type, "__args__", ())
            if args:
                return_type = args[0]

    output_schema = rich_to_arrow_schema({"result": return_type})

    # Store function traits in schema metadata for client discovery.
    if is_generator:
        output_schema.metadata["chalk.function.is_generator"] = "true"
    if is_async:
        output_schema.metadata["chalk.function.is_async"] = "true"

    return input_schema, output_schema


def _extract_field_names(schema_dict: dict | None) -> list[str]:
    """Extract field names from an Arrow schema JSON dict.

    Parameters
    ----------
    schema_dict
        Arrow schema JSON dict, or ``None``.

    Returns
    -------
    list of str
        Field names in declaration order.
    """
    if not schema_dict:
        return []
    return [f["name"] for f in schema_dict.get("columns", []) if "name" in f]


def _extract_schema_metadata(schema_dict: dict | None) -> dict[str, str]:
    """Return Arrow schema metadata from a Connect JSON schema dict."""
    if not schema_dict:
        return {}

    metadata = schema_dict.get("metadata") or {}
    if isinstance(metadata, dict):
        return {str(k): str(v) for k, v in metadata.items()}
    if isinstance(metadata, list):
        result: dict[str, str] = {}
        for entry in metadata:
            if isinstance(entry, dict) and "key" in entry:
                result[str(entry["key"])] = str(entry.get("value", ""))
        return result
    return {}


def _extract_function_traits(efv: dict[str, Any]) -> tuple[bool, bool]:
    """Return ``(is_generator, is_async)`` from catalog function metadata."""
    metadata = _extract_schema_metadata(efv.get("outputArrowSchema"))
    return (
        metadata.get("chalk.function.is_generator") == "true",
        metadata.get("chalk.function.is_async") == "true",
    )


def _normalize_schedule(schedule: str | None, *, owner: str = "schedule") -> str | None:
    if schedule is None:
        return None
    if not isinstance(schedule, str):
        raise TypeError(f"{owner} must be a string or None")
    normalized = schedule.strip()
    if not normalized:
        raise ValueError(f"{owner} must be a non-empty string")
    return normalized


def _callable_input_names(
    func: Callable[..., Any],
    *,
    skip_self: bool = False,
) -> list[str]:
    names: list[str] = []
    for param in inspect.signature(func).parameters.values():
        if skip_self and param.name == "self":
            continue
        names.append(param.name)
    return names


# ---------------------------------------------------------------------------
# RemoteFunction
# ---------------------------------------------------------------------------


class RemoteFunction:
    """A versioned remote function backed by ExternalFunctionCatalogService.

    Deploys a Python function as a ScalingGroup and registers it with
    input/output Arrow schemas derived from the function's type hints.

    Most users should prefer the :func:`function` decorator; use
    ``RemoteFunction`` directly when you need the imperative API (for
    example, when wrapping a function defined elsewhere, or when
    constructing deployments programmatically).

    Examples
    --------
    Decorator form — the most common entrypoint:

    >>> import chalkcompute
    >>> from chalkcompute import Image
    >>> @chalkcompute.function(
    ...     image=(
    ...         Image.debian_slim()
    ...         .pip_install(["numpy"])
    ...     ),
    ...     cpu="1",
    ...     memory="2Gi",
    ...     min_replicas=1,
    ...     max_replicas=4,
    ... )
    ... def embed(text: str) -> list[float]:
    ...     import numpy as np
    ...     return np.ones(8).tolist()
    >>> embed.deploy()
    >>> embed.remote("hello world")
    [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]

    Imperative form, for constructing a ``RemoteFunction`` from an existing
    callable:

    >>> from chalkcompute import RemoteFunction, Image
    >>> def square(x: int) -> int:
    ...     return x * x
    >>> fn = RemoteFunction(
    ...     square,
    ...     image=Image.debian_slim(),
    ...     min_replicas=1,
    ...     max_replicas=2,
    ... )
    >>> fn.deploy()
    >>> fn.remote(7)
    49
    >>> fn(7)  # runs the local Python body in-process
    49
    """

    def __init__(
        self,
        func: Callable[..., Any],
        *,
        image: _Image | None = None,
        name: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        gpu: str | None = None,
        env: dict[str, str] | None = None,
        secrets: list[_Secret] | None = None,
        volumes: list[tuple[str, str]] | None = None,
        min_replicas: int = 1,
        max_replicas: int = 1,
        shutdown_delay_seconds: int | None = None,
        scaling_interval_seconds: int | None = None,
        target_cpu_utilization_percentage: int | None = None,
        target_queue_depth: int | None = None,
        serialization_format: SerializationFormat = "pyarrow",
        options: dict[str, str] | None = None,
        max_buffer_duration: int | None = None,
        max_batching_size: int | None = None,
        retries: int | RetryPolicy | None = None,
        rate_limit: int | RateLimitPolicy | None = None,
        concurrency: int | ConcurrencyPolicy | None = None,
        quota: int | QueuePolicy | None = None,
        schedule: str | None = None,
        tracing: TracingPolicyConfig | None = None,
        models: typing.Mapping[str, Any] | None = None,
        chalk_client: Any | None = None,
    ) -> None:
        self._func = func
        self._has_local_func = True
        self._func_name = getattr(func, "__name__", "anonymous")
        if name is not None:
            validate_dns_label(name)
        self._explicit_name = name
        self._image = image
        if cpu is not None and not isinstance(cpu, str):
            raise TypeError(f"cpu must be a string such as '2' or '500m', got {cpu!r}")
        if memory is not None and not isinstance(memory, str):
            raise TypeError(
                f"memory must be a string such as '2Gi' or '512Mi', got {memory!r}"
            )
        if gpu is not None and not isinstance(gpu, str):
            raise TypeError(f"gpu must be a string such as 'nvidia-l4', got {gpu!r}")
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        self._env = dict(env) if env else {}
        validate_env(self._env)
        if models:
            from chalkcompute._model import MODELS_ENV_VAR, encode_models_env

            self._env[MODELS_ENV_VAR] = encode_models_env(models)
        self._secrets: list[_Secret] = list(secrets or [])
        referenced_volumes: list[tuple[str, str]] = []
        if volumes is not None:
            for item in volumes:
                if not (
                    isinstance(item, (tuple, list))
                    and len(item) == 2
                    and all(isinstance(s, str) for s in item)
                ):
                    raise ValueError(
                        f"Each volumes entry must be a (name, mount_path) string tuple, got {item!r}. "
                        f"Example: volumes=[('my-volume', '/data')]"
                    )
                # User-referenced volumes have no type of their own at
                # construction time. Their backend (hence mount type) is
                # resolved at deploy time via resolve_referenced_volume_type,
                # not defaulted here: a non-versioned chalkfs volume must not
                # be sent as versioned_chalkfs.
                referenced_volumes.append((item[0], item[1]))
        self._referenced_volumes: list[tuple[str, str]] = referenced_volumes
        # Populated at deploy time: referenced volumes (typed by lookup) plus
        # any freshly uploaded volumes (typed by the upload driver).
        self._volumes: list[VolumeMount] = []
        if min_replicas < 0:
            raise ValueError(f"min_replicas must be >= 0, got {min_replicas}")
        if max_replicas < 0:
            raise ValueError(f"max_replicas must be >= 0, got {max_replicas}")
        if min_replicas > max_replicas:
            raise ValueError(
                f"min_replicas ({min_replicas}) must be <= max_replicas ({max_replicas})"
            )
        self._volume_driver: VolumeDriver | None = None
        self._min_replicas = min_replicas
        self._max_replicas = max_replicas
        self._shutdown_delay_seconds = shutdown_delay_seconds
        self._scaling_interval_seconds = scaling_interval_seconds
        if target_cpu_utilization_percentage is not None and not (
            0 < target_cpu_utilization_percentage <= 100
        ):
            raise ValueError(
                f"target_cpu_utilization_percentage must be between 1 and 100, got {target_cpu_utilization_percentage}"
            )
        self._target_cpu_utilization_percentage = target_cpu_utilization_percentage
        self._target_queue_depth = target_queue_depth
        self._serialization_format: SerializationFormat = serialization_format
        self._options = options or {}
        self._max_buffer_duration = max_buffer_duration
        self._max_batching_size = max_batching_size
        self._schedule = _normalize_schedule(
            schedule,
            owner=f"Function '{self._func_name}' schedule",
        )
        if self._schedule is not None:
            input_names = _callable_input_names(func)
            if input_names:
                raise FunctionError(
                    f"Scheduled function '{self._func_name}' cannot have inputs: "
                    f"{', '.join(input_names)}"
                )
            if inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func):
                raise FunctionError(
                    f"Scheduled function '{self._func_name}' cannot be a generator; "
                    f"scheduled invocations have no caller to consume yielded values."
                )
        if max_batching_size is not None and (
            inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func)
        ):
            raise FunctionError(
                f"Function '{self._func_name}' is a generator; generators are "
                f"not supported with max_batching_size. Remove max_batching_size "
                f"or change the function to return a value."
            )
        self._retries = (
            RetryPolicy.from_int_or_policy(retries) if retries is not None else None
        )
        self._rate_limit = (
            RateLimitPolicy.from_int_or_policy(rate_limit)
            if rate_limit is not None
            else None
        )
        self._concurrency = (
            ConcurrencyPolicy.from_int_or_policy(concurrency)
            if concurrency is not None
            else None
        )
        self._quota = (
            QueuePolicy.from_int_or_policy(quota) if quota is not None else None
        )
        self._tracing = normalize_remote_function_tracing(tracing)

        # Set after .deploy()
        self._version_info: FunctionVersionInfo | None = None
        self._web_url: str | None = None
        self._port: int = 8080

        # Input field names (populated from API response for from_id/from_name)
        self._input_field_names: list[str] = []
        self._is_generator = inspect.isgeneratorfunction(
            func
        ) or inspect.isasyncgenfunction(func)
        self._is_async = inspect.iscoroutinefunction(
            func
        ) or inspect.isasyncgenfunction(func)

        # gRPC call channel (lazily created on first __call__)
        self._call_channel: Any = None
        self._call_stub: Any = None
        self._remote_call_client: Any = None
        self._queue_call_client: Any = None
        self._knowledge_cutoff: str | None = None

        self._client = ConnectClient(chalk_client=chalk_client)

        functools.update_wrapper(self, func)
        _preserve_functionlike_metadata(self, func)

    # -- Alternate constructors ------------------------------------------------

    @classmethod
    def from_id(
        cls, version_id: str, *, chalk_client: Any | None = None
    ) -> RemoteFunction:
        """Attach to an existing function version by ID.

        Parameters
        ----------
        version_id
            Identifier of the function version.

        Returns
        -------
        RemoteFunction
            A handle bound to the existing version.
        """
        f = cls._make_empty(chalk_client=chalk_client)
        resp = f._client.rpc(
            _CATALOG_SVC,
            "GetExternalFunctionVersion",
            {"id": version_id, "includeScalingGroup": True},
        )
        if resp.status_code != 200:
            raise FunctionError(f"GetExternalFunctionVersion failed: {resp.text}")
        data = resp.json()
        efv = data["externalFunctionVersion"]
        f._version_info = FunctionVersionInfo(
            id=efv["id"],
            function_name=efv["functionName"],
            version=efv.get("version", 0),
            scaling_group_name=efv.get("scalingGroupName", ""),
        )
        f._func_name = efv["functionName"]
        f._explicit_name = sanitize_dns_label(efv["functionName"])
        f._input_field_names = _extract_field_names(efv.get("inputArrowSchema"))
        f._is_generator, f._is_async = _extract_function_traits(efv)
        sg = data.get("scalingGroup")
        if sg:
            f._web_url = sg.get("webUrl")
            f._port = sg.get("port", 8080)
        return f

    @classmethod
    def from_name(cls, name: str, *, chalk_client: Any | None = None) -> RemoteFunction:
        """Attach to an existing function version by name.

        Parameters
        ----------
        name
            Name of the function.

        Returns
        -------
        RemoteFunction
            A handle bound to the existing version.
        """
        f = cls._make_empty(chalk_client=chalk_client)
        resp = f._client.rpc(
            _CATALOG_SVC,
            "GetExternalFunctionVersion",
            {"key": {"functionName": name}, "includeScalingGroup": True},
        )
        if resp.status_code != 200:
            raise FunctionError(f"GetExternalFunctionVersion failed: {resp.text}")
        data = resp.json()
        efv = data["externalFunctionVersion"]
        f._version_info = FunctionVersionInfo(
            id=efv["id"],
            function_name=efv["functionName"],
            version=efv.get("version", 0),
            scaling_group_name=efv.get("scalingGroupName", ""),
        )
        f._func_name = efv["functionName"]
        f._explicit_name = sanitize_dns_label(efv["functionName"])
        f._input_field_names = _extract_field_names(efv.get("inputArrowSchema"))
        f._is_generator, f._is_async = _extract_function_traits(efv)
        sg = data.get("scalingGroup")
        if sg:
            f._web_url = sg.get("webUrl")
            f._port = sg.get("port", 8080)
        return f

    @classmethod
    def _make_empty(cls, *, chalk_client: Any | None = None) -> RemoteFunction:
        f = cls.__new__(cls)
        f._func = lambda *a, **kw: None  # type: ignore[assignment]
        f._has_local_func = False
        f._func_name = "unknown"
        f._explicit_name = "unknown"
        f._image = None
        f._cpu = None
        f._memory = None
        f._gpu = None
        f._env = {}
        f._secrets = []
        f._referenced_volumes = []
        f._volumes = []
        f._volume_driver = None
        f._min_replicas = 1
        f._max_replicas = 1
        f._shutdown_delay_seconds = None
        f._scaling_interval_seconds = None
        f._target_cpu_utilization_percentage = None
        f._target_queue_depth = None
        f._serialization_format = "pyarrow"
        f._options = {}
        f._max_buffer_duration = None
        f._max_batching_size = None
        f._schedule = None
        f._retries = None
        f._rate_limit = None
        f._concurrency = None
        f._quota = None
        f._tracing = None
        f._version_info = None
        f._web_url = None
        f._port = 6666
        f._input_field_names = []
        f._is_generator = False
        f._is_async = False
        f._call_channel = None
        f._call_stub = None
        f._remote_call_client = None
        f._queue_call_client = None
        f._knowledge_cutoff = None
        f._client = ConnectClient(chalk_client=chalk_client)
        return f

    # -- Image building --------------------------------------------------------

    def _build_deploy_image(self) -> _Image:
        """Build the deploy image with handler shim and user module.

        Returns
        -------
        Image
            Declarative image ready to be built.
        """
        source_file, source_is_temp = _prepare_source_file(
            _source_file_for_deploy(self._func),
            label=f"Function {self._func_name!r}",
            entrypoint_sources=_entrypoint_source(self._func),
        )

        from chalkcompute._model import MODELS_ENV_VAR

        handler_shim = _generate_handler_shim(
            self._func_name,
            batched=self._max_batching_size is not None,
            bind_models=bool(self._env.get(MODELS_ENV_VAR)),
        )
        shim_path = _write_temp(handler_shim)

        deploy_image = (
            _make_base_image(
                self._image,
                remote_call_runtime_package=self._remote_call_runtime_package(),
            )
            .add_local_file(source_file, "/app/user_module.py")
            .add_local_file(shim_path, "/app/handler.py")
            .env({"PYTHONPATH": "/app", _REMOTE_ENV_VAR: "1"})
        )

        self._temp_files = [shim_path] + ([source_file] if source_is_temp else [])
        return deploy_image

    # -- Lifecycle -------------------------------------------------------------

    @_instrumented("chalkcompute.function.deploy")
    def deploy(
        self,
        *,
        poll_interval: float = 0.5,
        build_timeout: float = 600.0,
        ready_timeout: float = 300.0,
    ) -> RemoteFunction:
        """Build the image, register the function version, and wait until running.

        Parameters
        ----------
        poll_interval
            Seconds between status polls.
        build_timeout
            Maximum seconds to wait for the image build.
        ready_timeout
            Maximum seconds to wait for the scaling group to become ready.

        Returns
        -------
        RemoteFunction
            ``self`` for chaining.
        """
        from google.protobuf.json_format import MessageToDict

        from chalkcompute._progress import (
            progress_add,
            progress_done,
            progress_fail,
            progress_finish,
            progress_update,
        )

        fkey = f"function:{self._func_name}"
        progress_add(fkey, "function", f'Function "{self._func_name}"')

        try:
            # 1. Build deploy image
            deploy_image = self._build_deploy_image()

            # 2. Build image + upload lazy files
            image_uri, new_vols, driver = _build_and_upload(
                self._client,
                deploy_image,
                label=f"function '{self._func_name}'",
                volume_prefix="fn-files",
                poll_interval=poll_interval,
                build_timeout=build_timeout,
                progress_parent=fkey,
            )
            # Resolve each user-referenced volume's real mount type from its
            # backend instead of defaulting; a missing volume fails the deploy.
            referenced_volumes = [
                VolumeMount(
                    name=name,
                    mount_path=mount_path,
                    type=resolve_referenced_volume_type(self._client, name),
                )
                for name, mount_path in getattr(self, "_referenced_volumes", [])
            ]
            deploy_volumes = [
                *referenced_volumes,
                *new_vols,
            ]
            self._volumes = deploy_volumes
            self._volume_driver = driver
            _cleanup_temp_files(getattr(self, "_temp_files", []))
            self._temp_files = []

            # 3. Register function version
            input_schema_proto, output_schema_proto = _build_schemas(self._func)

            sg_name = sanitize_dns_label(self._explicit_name or self._func_name)

            handler_cmd = ["chalk-remote-call", "--handler", "handler.handler"]

            container_spec = _make_container_spec(
                name=sg_name,
                image_uri=image_uri,
                entrypoint=_local_install_entrypoint(handler_cmd),
                env=self._env,
                cpu=self._cpu,
                memory=self._memory,
                gpu=self._gpu,
                volumes=deploy_volumes,
            )
            scaling_spec: dict[str, Any] = {
                "minReplicas": self._min_replicas,
                "maxReplicas": self._max_replicas,
            }
            if self._shutdown_delay_seconds is not None:
                scaling_spec["shutdownDelaySeconds"] = self._shutdown_delay_seconds
            if self._scaling_interval_seconds is not None:
                scaling_spec["windowSeconds"] = self._scaling_interval_seconds
            if self._target_cpu_utilization_percentage is not None:
                scaling_spec["targetCpuUtilizationPercentage"] = (
                    self._target_cpu_utilization_percentage
                )
            if self._target_queue_depth is not None:
                scaling_spec["functionQueueDepthTrigger"] = {
                    "functionName": self._explicit_name or self._func_name,
                    "targetQueueDepth": self._target_queue_depth,
                }

            spec: dict[str, Any] = {
                "containerSpec": container_spec,
                "scalingSpec": scaling_spec,
            }

            # 5b. Resolve lazy secrets and add to container spec
            if self._secrets:
                from chalkcompute._secret import resolve_lazy_secrets

                if any(s.is_lazy for s in self._secrets):
                    resolved, _upserted = resolve_lazy_secrets(
                        self._secrets, self._client
                    )
                    self._secrets = resolved
                container_spec["secretRefs"] = [
                    s._to_proto_dict() for s in self._secrets
                ]

            # 5c. Build function-level config
            function_config: dict[str, Any] = {}
            if self._serialization_format != "pyarrow":
                function_config["serializationFormat"] = self._serialization_format
            if self._options:
                function_config["options"] = self._options
            if self._max_buffer_duration is not None:
                function_config["maxBufferDuration"] = self._max_buffer_duration
            if self._max_batching_size is not None:
                function_config["maxBatchingSize"] = self._max_batching_size
            if self._retries is not None:
                function_config["retryPolicy"] = self._retries.to_dict()
            if self._rate_limit is not None:
                function_config["rateLimit"] = self._rate_limit.to_dict()
            if self._concurrency is not None:
                function_config["concurrency"] = self._concurrency.to_dict()
            if self._quota is not None:
                function_config["queue"] = self._quota.to_dict()
            if self._schedule is not None:
                function_config["schedule"] = self._schedule
            if self._tracing is not None:
                function_config["tracing"] = self._tracing.to_dict()

            # 6. Call CreateExternalFunctionVersion
            rkey = f"register:{self._func_name}"
            progress_add(rkey, "function", "Registering function", parent=fkey)
            logger.info(
                "Registering function '%s'...", self._explicit_name or self._func_name
            )

            create_request: dict[str, Any] = {
                "functionName": self._explicit_name or self._func_name,
                "inputArrowSchema": MessageToDict(input_schema_proto),
                "outputArrowSchema": MessageToDict(output_schema_proto),
                "spec": spec,
            }
            if function_config:
                create_request["config"] = function_config

            resp = self._client.rpc(
                _CATALOG_SVC,
                "CreateExternalFunctionVersion",
                create_request,
                timeout=60,
            )
            if resp.status_code != 200:
                raise FunctionError(
                    f"CreateExternalFunctionVersion failed: {resp.text}"
                )

            data = resp.json()
            efv = data["externalFunctionVersion"]
            self._version_info = FunctionVersionInfo(
                id=efv["id"],
                function_name=efv["functionName"],
                version=efv.get("version", 0),
                scaling_group_name=efv.get("scalingGroupName", sg_name),
            )

            sg_data = data.get("scalingGroup")
            if sg_data:
                self._web_url = sg_data.get("webUrl")
                self._port = sg_data.get("port", 6666)
                sg_status = sg_data.get("status", "")
            else:
                self._port = 6666
                sg_status = ""

            logger.info(
                "Function version created: %s v%s (id=%s)",
                self._version_info.function_name,
                self._version_info.version,
                self._version_info.id,
            )
            progress_done(rkey)

            # 7. Poll scaling group until running
            if sg_status != "Running":
                progress_update(fkey, detail="waiting for ready")
                self._poll_until_running(
                    sg_name=self._version_info.scaling_group_name,
                    poll_interval=poll_interval,
                    timeout=ready_timeout,
                )

            progress_done(fkey, detail="ready")
        except BaseException:
            progress_fail(fkey)
            raise
        finally:
            progress_finish()

        return self

    def _remote_call_runtime_package(self) -> str:
        extras: list[str] = []
        if isinstance(self._tracing, TracingPolicy):
            extras.append("tracing")
        return _remote_call_runtime_package_with_extras(extras)

    def _poll_until_running(
        self,
        *,
        sg_name: str,
        poll_interval: float,
        timeout: float,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self._client.rpc(
                _SG_SVC,
                "GetScalingGroup",
                {"name": sg_name},
            )
            if resp.status_code != 200:
                raise FunctionError(f"GetScalingGroup failed: {resp.text}")
            sg = resp.json()["scalingGroup"]
            status = sg.get("status", "")
            status_message = sg.get("statusMessage", "")
            ready_replicas = sg.get("readyReplicas")
            available_replicas = sg.get("availableReplicas")
            self._web_url = sg.get("webUrl")
            logger.info(
                "  poll: status=%s ready=%s available=%s message=%s",
                status,
                ready_replicas,
                available_replicas,
                status_message or "",
            )
            if status in ("Running", "Available"):
                return
            if status in ("Failed", "Error"):
                message = f"Scaling group {sg_name} entered {status} state"
                if status_message:
                    message = f"{message}: {status_message}"
                raise FunctionError(message)
            time.sleep(poll_interval)
        message = f"Scaling group {sg_name} timed out after {timeout}s"
        if status_message:
            message = f"{message}: {status_message}"
        raise FunctionError(message)

    def wait_ready(
        self,
        *,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
    ) -> RemoteFunction:
        """Wait until the backing scaling group has at least one ready replica.

        Parameters
        ----------
        timeout
            Maximum seconds to wait.
        poll_interval
            Seconds between status polls.

        Returns
        -------
        RemoteFunction
            ``self`` for chaining.

        Raises
        ------
        FunctionError
            If the function has not been deployed or never becomes ready.
        """
        if self._version_info is None:
            raise FunctionError("No function version — call .deploy() first.")

        sg_name = self._version_info.scaling_group_name
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self._client.rpc(_SG_SVC, "GetScalingGroup", {"name": sg_name})
            if resp.status_code != 200:
                raise FunctionError(f"GetScalingGroup failed: {resp.text}")
            sg = resp.json()["scalingGroup"]
            status = sg.get("status", "")
            self._web_url = sg.get("webUrl")
            if status in ("Failed", "Error"):
                raise FunctionError(f"Scaling group {sg_name} entered {status} state")
            ready = sg.get("readyReplicas", 0)
            available = sg.get("availableReplicas", 0)
            if status in ("Running", "Available") and ready > 0 and available > 0:
                return self
            time.sleep(poll_interval)

        raise FunctionError(
            f"Function '{self._func_name}' not ready after {timeout}s (sg={sg_name})"
        )

    # -- Call ------------------------------------------------------------------

    @staticmethod
    def _remote_call_client_is_closed(client: Any) -> bool:
        return bool(
            getattr(client, "_closed", False) or getattr(client, "closed", False)
        )

    @staticmethod
    def _close_remote_call_client(client: Any) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.debug("Failed to close remote-call client", exc_info=True)

    def _drop_remote_call_clients(
        self,
        *,
        direct: bool = True,
        queue: bool = True,
    ) -> None:
        """Drop cached local call transports without touching server resources."""
        if direct:
            client = self._remote_call_client
            self._remote_call_client = None
            if client is not None:
                self._close_remote_call_client(client)

            channel = self._call_channel
            self._call_stub = None
            self._call_channel = None
            if channel is not None:
                self._close_remote_call_client(channel)

        if queue:
            client = self._queue_call_client
            self._queue_call_client = None
            if client is not None:
                self._close_remote_call_client(client)

    def _drop_remote_call_client(self, *, queue: bool) -> None:
        self._drop_remote_call_clients(direct=not queue, queue=queue)

    def _with_remote_call_transport(
        self,
        *,
        queue: bool,
        operation: str,
        call: Callable[[], _RemoteCallResult],
        retry_stale: bool | Callable[[BaseException], bool] = False,
        stale_result: Callable[[_RemoteCallResult], bool] | None = None,
    ) -> _RemoteCallResult:
        for attempt in range(2):
            try:
                result = call()
            except Exception as exc:
                if not _is_stale_remote_call_error(exc):
                    raise
                self._drop_remote_call_client(queue=queue)
                if attempt == 0 and _should_retry_stale_remote_call(retry_stale, exc):
                    logger.info(
                        "Detected stale remote-call client during %s for %r; "
                        "refreshing and retrying",
                        operation,
                        self._queue_function_name(),
                    )
                    continue
                raise

            if stale_result is not None and stale_result(result):
                self._drop_remote_call_client(queue=queue)
                if attempt == 0:
                    logger.info(
                        "Detected stale remote-call result during %s for %r; "
                        "refreshing and retrying",
                        operation,
                        self._queue_function_name(),
                    )
                    continue
                raise FunctionError(
                    f"Remote call {operation} failed after refreshing stale client"
                )
            return result

        raise FunctionError(
            f"Remote call {operation} failed after refreshing stale client"
        )

    def _ensure_call_stub(self) -> None:
        """Lazily create the gRPC channel and stub for calling the remote function.

        Raises
        ------
        FunctionError
            If the function has not been deployed.
        """
        if self._call_stub is not None:
            return

        import grpc
        from chalk_remote_call._gen.chalk.runtime.v1 import (
            remote_python_call_pb2_grpc as call_grpc,
        )

        if self._web_url is None:
            raise FunctionError("Function is not deployed. Call .deploy() first.")

        target, use_tls = grpc_target_from_url(
            self._web_url,
            insecure_port=self._port,
        )

        if use_tls:
            self._call_channel = grpc.secure_channel(
                target, grpc.ssl_channel_credentials()
            )
        else:
            self._call_channel = grpc.insecure_channel(target)
        self._call_stub = call_grpc.RemoteCallServiceStub(self._call_channel)

    def _remote_call_metadata(self, *, queue: bool) -> list[tuple[str, str]]:
        service_auth = service_auth_context_from_env()
        if service_auth is not None:
            metadata = [("x-chalk-env-id", service_auth.environment_id)]
            if queue:
                metadata.append(("x-chalk-deployment-type", "function-queue"))
            return metadata

        self._client._ensure_auth()
        metadata = [
            ("authorization", f"Bearer {self._client._access_token}"),
            ("x-chalk-env-id", self._client._env_id or ""),
        ]
        if queue:
            metadata.append(("x-chalk-deployment-type", "function-queue"))
        return metadata

    def _per_call_metadata(self) -> list[tuple[str, str]]:
        metadata = current_trace_metadata()
        if self._knowledge_cutoff is not None:
            metadata.append((_REQUEST_TIMESTAMP_HEADER, self._knowledge_cutoff))
        return metadata

    def _ensure_remote_call_client(self, *, queue: bool = False) -> Any:
        attr = "_queue_call_client" if queue else "_remote_call_client"
        client = getattr(self, attr)
        if client is not None and not self._remote_call_client_is_closed(client):
            return client
        if client is not None:
            setattr(self, attr, None)

        if self._web_url is None:
            raise FunctionError("Function is not deployed. Call .deploy() first.")

        from chalkcompute._remote_call_client import RemoteCallClient

        service_auth = service_auth_context_from_env()
        if queue:
            if service_auth is None:
                target, use_tls = self._client.grpc_engine_target()
            else:
                if service_auth.grpc_engine is None:
                    target, use_tls = self._client.grpc_engine_target()
                else:
                    target, use_tls = service_auth.grpc_engine
        else:
            target, use_tls = grpc_target_from_url(
                self._web_url,
                insecure_port=self._port,
            )
        client = RemoteCallClient(
            target,
            metadata=self._remote_call_metadata(queue=queue),
            use_tls=use_tls,
            auth_endpoint=service_auth.auth_endpoint if service_auth else None,
            exchange_token=service_auth.exchange_token if service_auth else None,
            requested_permissions=(
                service_auth.requested_permissions if service_auth else None
            ),
            initial_poll_delay_ms=5,
        )
        setattr(self, attr, client)
        return client

    def _call_direct_chunks_python_grpc(
        self,
        feather_bytes: bytes,
        function_name: str,
        metadata: list[tuple[str, str]],
    ) -> list[bytes]:
        from chalk_remote_call._gen.chalk.runtime.v1 import (
            remote_python_call_pb2 as call_pb,
        )

        self._ensure_call_stub()

        def requests() -> Any:
            yield call_pb.CallFunctionRequest(  # ty: ignore[unresolved-attribute]
                name=function_name,
                feather_stream=feather_bytes,
            )

        responses = list(self._call_stub.CallFunction(requests(), metadata=metadata))
        return [resp.feather_stream for resp in responses]

    def _call_direct_chunks_once(
        self,
        feather_bytes: bytes,
        function_name: str,
        metadata: list[tuple[str, str]],
    ) -> list[bytes]:
        from chalkcompute._remote_call_client import RemoteCallClientUnavailable

        try:
            client = self._ensure_remote_call_client(queue=False)
        except RemoteCallClientUnavailable:
            fallback_metadata = [
                *self._remote_call_metadata(queue=False),
                *metadata,
            ]
            return self._call_direct_chunks_python_grpc(
                feather_bytes,
                function_name,
                fallback_metadata,
            )
        return client.call_ipc(function_name, feather_bytes, metadata=metadata)

    def _call_direct_chunks(self, feather_bytes: bytes) -> list[bytes]:
        function_name = self._queue_function_name()
        metadata = self._per_call_metadata()
        return self._with_remote_call_transport(
            queue=False,
            operation="direct call",
            retry_stale=_is_retryable_stale_remote_call_error,
            call=lambda: self._call_direct_chunks_once(
                feather_bytes,
                function_name,
                metadata,
            ),
        )

    def _queue_function_name(self) -> str:
        if self._version_info is not None and self._version_info.function_name:
            return self._version_info.function_name
        return self._func_name

    def _serialize_call_args(self, *args: Any, **kwargs: Any) -> bytes:
        import pyarrow as pa
        import pyarrow.ipc as ipc

        if self._input_field_names:
            arguments: dict[str, Any] = {}
            for i, name in enumerate(self._input_field_names):
                if name in kwargs:
                    arguments[name] = kwargs[name]
                elif i < len(args):
                    arguments[name] = args[i]
                else:
                    raise TypeError(f"Missing argument: {name}")
        else:
            sig = inspect.signature(self._func)
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = {
                name: value
                for name, value in bound.arguments.items()
                if sig.parameters[name].kind
                not in (
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                )
            }

        arrays = []
        names = []
        if arguments:
            for param_name, value in arguments.items():
                arrays.append(pa.array([value]))
                names.append(param_name)
        else:
            arrays.append(pa.array([True]))
            names.append(_NO_ARGS_SENTINEL_COLUMN)
        batch = pa.record_batch(arrays, names=names)

        sink = pa.BufferOutputStream()
        with ipc.new_stream(sink, batch.schema) as writer:
            writer.write_batch(batch)
        return sink.getvalue().to_pybytes()

    def _decode_result_chunks(self, chunks: list[bytes]) -> list[Any]:
        import pyarrow.ipc as ipc

        results: list[Any] = []
        for chunk in chunks:
            reader = ipc.open_stream(chunk)
            for result_batch in reader:
                result_dict = result_batch.to_pydict()
                if "result" in result_dict:
                    results.extend(result_dict["result"])
        return results

    def _materialize_result_chunks(self, chunks: list[bytes]) -> Any:
        results = self._decode_result_chunks(chunks)
        if len(results) == 1:
            return results[0]
        return results

    def _call_via_api_rpc(self, feather_bytes: bytes) -> bytes:
        """Call the remote function via the API server's ``CallExternalFunction`` RPC.

        Parameters
        ----------
        feather_bytes
            Arrow IPC (Feather) bytes for the request.

        Returns
        -------
        bytes
            Response Arrow IPC bytes.
        """
        import base64

        if self._version_info is None:
            raise FunctionError("No function version — call .deploy() first.")

        function_key: dict[str, Any] = {
            "functionName": self._version_info.function_name,
        }
        if self._version_info.version:
            function_key["version"] = self._version_info.version

        request_body: dict[str, Any] = {
            "function": function_key,
            "remoteCallRequest": {
                "name": self._queue_function_name(),
                "featherStream": base64.b64encode(feather_bytes).decode("ascii"),
            },
        }

        trace_metadata = self._per_call_metadata()
        rpc_kwargs: dict[str, Any] = {"timeout": 300}
        if trace_metadata:
            rpc_kwargs["extra_headers"] = dict(trace_metadata)
        resp = self._client.rpc(
            _CATALOG_SVC,
            "CallExternalFunction",
            request_body,
            **rpc_kwargs,
        )
        if resp.status_code != 200:
            raise FunctionError(f"CallExternalFunction failed: {resp.text}")

        data = resp.json()
        remote_resp = data.get("remoteCallResponse")
        if not remote_resp:
            raise FunctionError(
                "No remoteCallResponse in CallExternalFunction response"
            )

        feather_b64 = remote_resp.get("featherStream")
        if not feather_b64:
            raise FunctionError("No featherStream in CallExternalFunction response")

        return base64.b64decode(feather_b64)

    def with_knowledge_cutoff(
        self, knowledge_cutoff: KnowledgeCutoff
    ) -> RemoteFunction:
        """Return a call-scoped function handle with an overridden request time.

        The returned handle shares this function's deployment and clients but sends
        ``x-chalk-request-timestamp`` on subsequent calls and deferred enqueues.
        """
        clone = self.__class__.__new__(self.__class__)
        clone.__dict__ = self.__dict__.copy()
        clone._knowledge_cutoff = _format_knowledge_cutoff(knowledge_cutoff)
        return clone

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the underlying Python function locally, in-process.

        This runs the original function body in the current process — it does
        **not** dispatch to the deployed remote function. Use :meth:`remote`
        for a synchronous wire call, or :meth:`defer` to enqueue one and get a
        handle back.
        """
        if not self._has_local_func:
            raise FunctionError(
                f"{self._func_name!r} was attached via from_name()/from_id() and "
                f"has no local implementation to run in-process. Use .remote() "
                f"for a synchronous wire call, or .defer() to enqueue one."
            )
        return self._func(*args, **kwargs)

    def remote(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the deployed remote function over the wire.

        Parameters
        ----------
        *args
            Positional arguments to forward to the remote function.
        **kwargs
            Keyword arguments to forward to the remote function.

        Returns
        -------
        Any
            Scalar result for single-row calls, or a list of results otherwise.
            For generator functions, returns an iterator of result batches.
        """
        if self._is_generator:
            return self.defer(*args, **kwargs).stream()
        if _has_running_event_loop():
            return self._call_async(*args, **kwargs)
        return self._call_sync(*args, **kwargs)

    async def _call_async(self, *args: Any, **kwargs: Any) -> Any:
        return await _run_in_worker_thread(self._call_sync, *args, **kwargs)

    def _call_sync(self, *args: Any, **kwargs: Any) -> Any:
        feather_bytes = self._serialize_call_args(*args, **kwargs)
        call_type = "api" if _should_call_via_api() else "direct"

        with remote_function_call_span(self._queue_function_name(), call_type):
            if _should_call_via_api():
                response_bytes = self._call_via_api_rpc(feather_bytes)
                results = self._decode_result_chunks([response_bytes])
            else:
                chunks = self._call_direct_chunks(feather_bytes)
                if not chunks:
                    raise FunctionError("No response received from remote function")
                results = self._decode_result_chunks(chunks)

        if len(results) == 1:
            return results[0]
        return results

    def _enqueue(self, feather_bytes: bytes) -> str:
        """Enqueue a pre-serialized call payload and return its call id.

        Shared by :meth:`defer` and :meth:`FunctionCallHandle` resubmission, so a
        retried call goes through the exact same queue path (fresh per-call
        metadata included).
        """
        function_name = self._queue_function_name()
        with remote_function_call_span(function_name, "queue"):
            return self._with_remote_call_transport(
                queue=True,
                operation="enqueue",
                call=lambda: self._ensure_remote_call_client(queue=True).enqueue(
                    function_name,
                    feather_bytes,
                    metadata=self._per_call_metadata(),
                ),
            )

    def defer(self, *args: Any, **kwargs: Any) -> FunctionCallHandle:
        """Enqueue this function call and return a handle immediately."""
        feather_bytes = self._serialize_call_args(*args, **kwargs)
        call_id = self._enqueue(feather_bytes)
        return FunctionCallHandle(self, call_id, request_bytes=feather_bytes)

    def poll(
        self,
        call_id: str,
        *,
        cursor: str = "",
        poll_count: int = 0,
    ) -> Any:
        """Poll a deferred call once."""
        return self._with_remote_call_transport(
            queue=True,
            operation="poll",
            retry_stale=True,
            stale_result=lambda result: _is_stale_remote_call_error_message(
                result.error_message
            ),
            call=lambda: self._ensure_remote_call_client(queue=True).poll_next(
                call_id,
                cursor,
                poll_count,
            ),
        )

    def purge(self) -> int:
        """Drop pending queued calls for this function."""
        return int(
            self._with_remote_call_transport(
                queue=True,
                operation="purge",
                call=lambda: self._ensure_remote_call_client(queue=True).purge_function(
                    self._queue_function_name()
                ),
            )
        )

    # -- Management ------------------------------------------------------------

    @_instrumented("chalkcompute.function.delete")
    def delete(self) -> None:
        """Delete this function version."""
        if self._version_info is not None:
            self._client.rpc(
                _CATALOG_SVC,
                "DeleteExternalFunctionVersion",
                {"id": self._version_info.id},
            )
            self._version_info = None
            self._web_url = None
            self._drop_remote_call_clients()

    def refresh(self) -> FunctionVersionInfo:
        """Fetch latest function version info from the server.

        Returns
        -------
        FunctionVersionInfo
            Updated version info.

        Raises
        ------
        FunctionError
            If the function has not been deployed or the RPC fails.
        """
        if self._version_info is None:
            raise FunctionError("No function version — call .deploy() first.")
        old_web_url = self._web_url
        old_port = self._port
        resp = self._client.rpc(
            _CATALOG_SVC,
            "GetExternalFunctionVersion",
            {"id": self._version_info.id, "includeScalingGroup": True},
        )
        if resp.status_code != 200:
            raise FunctionError(f"GetExternalFunctionVersion failed: {resp.text}")
        data = resp.json()
        efv = data["externalFunctionVersion"]
        self._version_info = FunctionVersionInfo(
            id=efv["id"],
            function_name=efv["functionName"],
            version=efv.get("version", 0),
            scaling_group_name=efv.get("scalingGroupName", ""),
        )
        sg = data.get("scalingGroup")
        if sg:
            self._web_url = sg.get("webUrl")
            self._port = sg.get("port", self._port)
        if self._web_url != old_web_url or self._port != old_port:
            self._drop_remote_call_clients(direct=True, queue=False)
        return self._version_info

    @property
    def version_info(self) -> FunctionVersionInfo | None:
        return self._version_info

    @property
    def web_url(self) -> str | None:
        return self._web_url

    def __repr__(self) -> str:
        v = f" v{self._version_info.version}" if self._version_info else ""
        deployed = "deployed" if self._version_info else "not deployed"
        return f"RemoteFunction({self._func_name!r}{v}, {deployed})"


# ---------------------------------------------------------------------------
# No-op wrapper
# ---------------------------------------------------------------------------


class _NoOpFunction:
    """The decorated symbol as seen *inside* a deployed container.

    Calling it (``add(1, 2)``) runs the original Python body in-process, exactly
    like a plain function. ``.remote()`` / ``.defer()`` dispatch to the deployed
    remote function, resolved lazily by name — so a function can invoke a sibling
    without an explicit ``RemoteFunction.from_name(...)``.
    """

    def __init__(self, func: Callable, *, name: str | None = None) -> None:
        self._func = func
        self._registered_name = name or getattr(func, "__name__", "anonymous")
        self._remote: RemoteFunction | None = None
        functools.update_wrapper(self, func)
        _preserve_functionlike_metadata(self, func)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._func(*args, **kwargs)

    def _bound(self) -> RemoteFunction:
        """Lazily attach to the deployed function by its registered name."""
        if self._remote is None:
            self._remote = RemoteFunction.from_name(self._registered_name)
        return self._remote

    def remote(self, *args: Any, **kwargs: Any) -> Any:
        return self._bound().remote(*args, **kwargs)

    def defer(self, *args: Any, **kwargs: Any) -> FunctionCallHandle:
        return self._bound().defer(*args, **kwargs)

    def with_knowledge_cutoff(
        self, knowledge_cutoff: KnowledgeCutoff
    ) -> RemoteFunction:
        return self._bound().with_knowledge_cutoff(knowledge_cutoff)

    def deploy(self, **_: Any) -> DeployedFunction:
        return self

    def wait_ready(self, **_: Any) -> DeployedFunction:
        return self

    def delete(self) -> None:
        pass

    def refresh(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Decorator (convenience wrapper)
# ---------------------------------------------------------------------------


@overload
def function(
    func: Callable[..., Any],
    /,
    *,
    image: _Image | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    gpu: str | None = None,
    name: str | None = None,
    env: dict[str, str] | None = None,
    secrets: list[_Secret] | None = None,
    volumes: list[tuple[str, str]] | None = None,
    min_replicas: int = 1,
    max_replicas: int = 1,
    shutdown_delay_seconds: int | None = None,
    scaling_interval_seconds: int | None = None,
    target_cpu_utilization_percentage: int | None = None,
    target_queue_depth: int | None = None,
    serialization_format: SerializationFormat = "pyarrow",
    options: dict[str, str] | None = None,
    max_buffer_duration: int | None = None,
    max_batching_size: int | None = None,
    retries: int | RetryPolicy | None = None,
    rate_limit: int | RateLimitPolicy | None = None,
    concurrency: int | ConcurrencyPolicy | None = None,
    quota: int | QueuePolicy | None = None,
    schedule: str | None = None,
    models: typing.Mapping[str, Any] | None = None,
    tracing: TracingPolicyConfig | None = None,
) -> RemoteFunction | _NoOpFunction: ...


@overload
def function(
    *,
    image: _Image | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    gpu: str | None = None,
    name: str | None = None,
    env: dict[str, str] | None = None,
    secrets: list[_Secret] | None = None,
    volumes: list[tuple[str, str]] | None = None,
    min_replicas: int = 1,
    max_replicas: int = 1,
    shutdown_delay_seconds: int | None = None,
    scaling_interval_seconds: int | None = None,
    target_cpu_utilization_percentage: int | None = None,
    target_queue_depth: int | None = None,
    serialization_format: SerializationFormat = "pyarrow",
    options: dict[str, str] | None = None,
    max_buffer_duration: int | None = None,
    max_batching_size: int | None = None,
    retries: int | RetryPolicy | None = None,
    rate_limit: int | RateLimitPolicy | None = None,
    concurrency: int | ConcurrencyPolicy | None = None,
    quota: int | QueuePolicy | None = None,
    schedule: str | None = None,
    models: typing.Mapping[str, Any] | None = None,
    tracing: TracingPolicyConfig | None = None,
) -> Callable[..., RemoteFunction | _NoOpFunction]: ...


def function(
    func: Callable[..., Any] | None = None,
    /,
    *,
    image: _Image | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    gpu: str | None = None,
    name: str | None = None,
    env: dict[str, str] | None = None,
    secrets: list[_Secret] | None = None,
    volumes: list[tuple[str, str]] | None = None,
    min_replicas: int = 1,
    max_replicas: int = 1,
    shutdown_delay_seconds: int | None = None,
    scaling_interval_seconds: int | None = None,
    target_cpu_utilization_percentage: int | None = None,
    target_queue_depth: int | None = None,
    serialization_format: SerializationFormat = "pyarrow",
    options: dict[str, str] | None = None,
    max_buffer_duration: int | None = None,
    max_batching_size: int | None = None,
    retries: int | RetryPolicy | None = None,
    rate_limit: int | RateLimitPolicy | None = None,
    concurrency: int | ConcurrencyPolicy | None = None,
    quota: int | QueuePolicy | None = None,
    schedule: str | None = None,
    models: typing.Mapping[str, Any] | None = None,
    tracing: TracingPolicyConfig | None = None,
) -> Callable[..., RemoteFunction | _NoOpFunction] | RemoteFunction | _NoOpFunction:
    """Decorator that deploys a function as a versioned remote function.

    **Locally** (at decoration time):

    1. Derives Arrow schemas from the function's type hints
    2. Builds a deploy image with a handler shim
    3. Registers the function version via ``ExternalFunctionCatalogService``
    4. If ``CHALK_FUNCTION_LOCAL_SDK_COPY=1``, includes local SDK source in
       the deploy image for unreleased feature iteration.

    **Remotely** (inside the container):
    Lifecycle methods become no-ops. Calling the symbol runs the local Python
    body in-process; ``.remote()`` / ``.defer()`` dispatch to the deployed
    function (resolved lazily by name), so a function can invoke a sibling
    without ``RemoteFunction.from_name(...)``.

    Parameters
    ----------
    image
        Container image to deploy in. Defaults to ``Image.debian_slim()``.
    cpu
        CPU resource request (e.g. ``"1"``, ``"500m"``).
    memory
        Memory resource request (e.g. ``"1Gi"``, ``"512Mi"``).
    gpu
        GPU resource request (e.g. ``"nvidia-l4"``, ``"2:nvidia-a100"``).
        Format is ``"<count>:<type>"`` or just ``"<type>"`` for a single GPU.
    name
        Custom function name. Defaults to the function's ``__name__``.
    env
        Environment variables to pass to the function.
    secrets
        List of :class:`~chalkcompute.Secret` references to inject.
    volumes
        List of ``(name, mount_path)`` tuples for persistent storage.
    min_replicas
        Minimum number of replicas.
    max_replicas
        Maximum number of replicas.
    shutdown_delay_seconds
        Graceful termination period in seconds before replicas are forcibly
        killed during scale-down. Defaults to 30s on the server.
    scaling_interval_seconds
        How often (in seconds) the autoscaler scrapes metrics to make scaling
        decisions. Defaults to 60s on the server.
    target_cpu_utilization_percentage
        Target CPU utilization percentage (0-100) that drives autoscaling.
    target_queue_depth
        Target pending-request queue depth per replica. When set, the
        autoscaler scales on the function's own queue depth.
    serialization_format
        Wire format for arguments and results. Currently only ``"pyarrow"``
        is supported.
    options
        Arbitrary key-value options forwarded to the Chalk platform.
    max_buffer_duration
        Maximum time in milliseconds to buffer incoming items before invoking
        the handler. Defaults to 1000 ms when batching is enabled. When
        batching is enabled, handler args are lists.
    max_batching_size
        Maximum number of items to accumulate before invoking the handler.
        Defaults to 10 when batching is enabled.
    retries
        Retry policy for handler invocations. Pass an ``int`` for simple
        max-attempts with default exponential backoff, or a
        :class:`~chalkcompute.RetryPolicy` for full control. Enforced
        before each outbound RPC, not in the handler.
    rate_limit
        Rate limit policy for outbound calls. Pass an ``int`` for a simple
        "N per second" cap with a per-function key, or a
        :class:`~chalkcompute.RateLimitPolicy` for full control (``rate``,
        ``per``, ``key``). Multiple functions sharing the same ``key`` share
        one bucket — used when a downstream service imposes a throughput
        limit across callers.
    concurrency
        Concurrency policy: cap on in-flight handler invocations. Pass an
        ``int`` for a simple ``max_concurrent`` cap with a per-function key,
        or a :class:`~chalkcompute.ConcurrencyPolicy` for full control
        (``max_concurrent``, ``key``). Multiple functions sharing the same
        ``key`` share one gate — independent of ``rate_limit`` (which caps
        rate, not in-flight count).
    quota
        Queue quota: cap on the number of pending items that may be enqueued
        for this function at once. Pass an ``int`` for the cap, or a
        :class:`~chalkcompute.QueuePolicy` (currently equivalent —
        ``QueuePolicy(max_items=N)``). Defaults to 500,000 items when unset.
        Submitting past the cap raises a gRPC ``RESOURCE_EXHAUSTED`` error —
        clients should back off or call ``purge()`` on the function to drain
        the queue if items were erroneously added.
    schedule
        Optional crontab or Chalk duration string for invoking a no-input
        function on a schedule (e.g. ``"0 * * * *"`` or ``"1h"``).
    models
        Mapping of ``model_name -> output_type`` for Chalk models that should
        be auto-bound into the local chalkdf catalog before the function body
        runs. Each entry becomes a :func:`~chalkcompute.bind_model` call
        invoked by the handler shim on import, so
        ``F.catalog_call("model.<name>", ..., output_type=<type>)`` resolves
        without any explicit setup in the function body. For advanced overrides
        (``version``, ``method_name``, ``qualified_name``) call
        :func:`~chalkcompute.bind_model` directly inside the function.
    tracing
        Server-side remote-function tracing is enabled by default. Use
        :class:`~chalkcompute.TracingPolicy` for sampling. Set ``False`` to
        explicitly turn tracing off.

    Examples
    --------
    >>> import chalkcompute
    >>> @chalkcompute.function
    ... def add(x: int, y: int) -> int:
    ...     return x + y
    >>> add.remote(1, 2)   # wire call to the deployed function
    >>> add(1, 2)          # runs the Python body in-process
    """

    def decorator(func: Callable) -> RemoteFunction | _NoOpFunction:
        if os.environ.get(_REMOTE_ENV_VAR):
            return _NoOpFunction(func, name=name)

        f = RemoteFunction(
            func,
            image=image,
            name=name,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            env=env,
            secrets=secrets,
            volumes=volumes,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            shutdown_delay_seconds=shutdown_delay_seconds,
            scaling_interval_seconds=scaling_interval_seconds,
            target_cpu_utilization_percentage=target_cpu_utilization_percentage,
            target_queue_depth=target_queue_depth,
            serialization_format=serialization_format,
            options=options,
            max_buffer_duration=max_buffer_duration,
            max_batching_size=max_batching_size,
            retries=retries,
            rate_limit=rate_limit,
            concurrency=concurrency,
            quota=quota,
            schedule=schedule,
            tracing=tracing,
            models=models,
        )
        f.deploy()
        return f

    if func is not None:
        if not callable(func):
            raise TypeError("function() positional argument must be callable")
        return decorator(func)

    return decorator
