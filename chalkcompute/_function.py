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
import atexit
import asyncio
import contextlib
import contextvars
import datetime as _dt
import functools
import inspect
import json
import linecache
import logging
import os
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import traceback
import typing
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
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
from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from connectrpc.protocol import ProtocolType
from protobuf import Oneof
from protobuf.wkt import FieldMask

from chalkcompute import _local_dev
from chalkcompute._concurrency import ConcurrencyPolicy
from chalkcompute._connect import (
    ConnectClient,
    grpc_target_from_url,
    rpc_address_from_target,
    service_auth_context_from_env,
)
from chalkcompute._containerspec import ContainerSpec, validate_replica_and_utilization
from chalkcompute._deployment_proto import function_config, scaling_spec
from chalkcompute._function_deployment import (
    ensure_external_function,
    prepare_function_image,
)
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import (
    service_connect as catalog_connect,
)
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as catalog_pb
from chalkcompute._gen.chalk.runtime.v1 import remote_python_call_pb as remote_call_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_connect as sg_connect
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._http import SYNC_HTTP_CLIENT
from chalkcompute._naming import (
    sanitize_dns_label,
    validate_dns_label,
)
from chalkcompute._quota import QueuePolicy
from chalkcompute._rate_limit import RateLimitPolicy
from chalkcompute._remote_function_tracing import (
    TracingPolicy,
    TracingPolicyConfig,
    normalize_remote_function_tracing,
)
from chalkcompute._retry import RetryPolicy
from chalkcompute._rpc import RPC_READ_MAX_BYTES, rpc_interceptors
from chalkcompute._secret import prepare_lazy_secrets
from chalkcompute._tracing import (
    _instrumented,
    current_trace_metadata,
    remote_function_call_span,
)
from chalkcompute._volume import (
    Volume,
    VolumeClient,
    VolumeMount,
    normalize_volume_refs,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from chalkcompute._image import Image as _Image
    from chalkcompute._scaling_group import CronScalingTrigger as _CronScalingTrigger
    from chalkcompute._scaling_group import ReadinessProbe as _ReadinessProbe
    from chalkcompute._scaling_group import (
        ScaleFromZeroRequestPolicy as _ScaleFromZeroRequestPolicy,
    )
    from chalkcompute._secret import Secret as _Secret

SerializationFormat = typing.Literal["pyarrow"]
F = TypeVar("F", bound=Callable[..., Any])
_RemoteCallResult = TypeVar("_RemoteCallResult")

KnowledgeCutoff = str | _dt.datetime

_REMOTE_ENV_VAR = "CHALK_REMOTE_FUNCTION"
_FNQ_STREAMS_SUPPORTED_ENV_VAR = "CHALK_FNQ_STREAMS_SUPPORTED"
_REQUEST_TIMESTAMP_HEADER = "x-chalk-request-timestamp"
_NO_ARGS_SENTINEL_COLUMN = "__chalk_no_args__"
# Reserved column carrying the caller's per-row metadata blob. Stripped from the argument
# mapping by the handler shim and published through a context variable instead, so a
# function can read it without declaring a parameter for it.
_ROW_METADATA_COLUMN = "__chalk_row_metadata__"
_LOCAL_COPY_ENV_VAR = _local_dev._LOCAL_COPY_ENV_VAR
_LOCAL_WHEEL_ENV_VAR = _local_dev._LOCAL_WHEEL_ENV_VAR
_LOCAL_REMOTE_CALL_WHEEL_ENV_VAR = _local_dev._LOCAL_REMOTE_CALL_WHEEL_ENV_VAR
_STRIP_SOURCE_ENV_VAR = _local_dev._STRIP_SOURCE_ENV_VAR
# When enabled, route __call__ through the API server's CallExternalFunction
# RPC instead of connecting directly to the scaling group's web URL.
_CALL_VIA_API_ENV_VAR = "CHALK_FUNCTION_CALL_VIA_API"


def _remote_call_container_env(user_env: dict[str, str]) -> dict[str, str]:
    return {
        "PYTHONPATH": "/app",
        _REMOTE_ENV_VAR: "1",
        _FNQ_STREAMS_SUPPORTED_ENV_VAR: "true",
        **user_env,
    }


def _remote_function_startup_probe() -> dict[str, dict[str, object]]:
    return {"grpc": {}}


# ---------------------------------------------------------------------------
# Background decorator deployments
# ---------------------------------------------------------------------------


_background_deploy_executor: ThreadPoolExecutor | None = None
_background_deploy_executor_lock = threading.Lock()
_background_deployments_lock = threading.Lock()
_background_deployment_tails: dict[str, Future[None]] = {}
_unobserved_background_deployments: dict[Future[None], str] = {}


def _get_background_deploy_executor() -> ThreadPoolExecutor:
    """Return the process-wide pool used by ``@function`` deployments.

    A pre-shutdown hook drains this pool before Python disables executor
    submissions globally. A script containing only decorated function definitions
    therefore waits for every queued deployment, while deployments may still use
    their own worker pools during teardown.
    """
    global _background_deploy_executor
    with _background_deploy_executor_lock:
        if _background_deploy_executor is None:
            _background_deploy_executor = ThreadPoolExecutor(
                thread_name_prefix="chalk-function-deploy"
            )
        return _background_deploy_executor


def _await_deploy_predecessor(predecessor: Future[None] | None) -> None:
    """Wait for the previous deploy of a registered name, ignoring its outcome.

    The chain exists to order version selection, not to propagate failures: a
    deploy that inherited its predecessor's exception would never run its own
    attempt, and would report an error belonging to someone else's.
    """
    if predecessor is None:
        return
    try:
        predecessor.result()
    except Exception:
        pass


@contextlib.contextmanager
def _manual_deploy_slot(registered_name: str) -> typing.Iterator[None]:
    """Hold a caller-initiated deploy's place in its registered name's chain.

    Without a slot a manual ``deploy()`` is invisible to the chain, so a
    function decorated while one is in flight would select a version
    concurrently with it.

    Publishing and releasing are paired in one context manager because
    everything after publication must release: waiting on the predecessor can
    raise something ``_await_deploy_predecessor`` deliberately does not swallow
    (a ``KeyboardInterrupt``, say), and an unreleased barrier would block every
    later deploy of that name forever — including at interpreter exit, where
    the executor shutdown waits on queued deployments.
    """
    barrier: Future[None] = Future()
    barrier.set_running_or_notify_cancel()
    with _background_deployments_lock:
        predecessor = _background_deployment_tails.get(registered_name)
        _background_deployment_tails[registered_name] = barrier
    try:
        _await_deploy_predecessor(predecessor)
        yield
    finally:
        with _background_deployments_lock:
            if _background_deployment_tails.get(registered_name) is barrier:
                _background_deployment_tails.pop(registered_name, None)
        if not barrier.done():
            barrier.set_result(None)


def _schedule_background_deployment(function: RemoteFunction) -> Future[None]:
    """Schedule one decorator-triggered deployment and return its barrier.

    Deployments for distinct registered names may overlap. Deployments selecting
    versions of the same stable function are chained in declaration order so
    their final traffic selection remains deterministic.
    """
    executor = _get_background_deploy_executor()
    context = contextvars.copy_context()
    registered_name = function._explicit_name or function._func_name

    with _background_deployments_lock:
        predecessor = _background_deployment_tails.get(registered_name)

        def run() -> None:
            _await_deploy_predecessor(predecessor)
            context.run(function._deploy)

        future = executor.submit(run)
        _background_deployment_tails[registered_name] = future
        _unobserved_background_deployments[future] = registered_name

    def completed(done: Future[None]) -> None:
        with _background_deployments_lock:
            if _background_deployment_tails.get(registered_name) is done:
                _background_deployment_tails.pop(registered_name, None)
            if not done.cancelled() and done.exception() is None:
                _unobserved_background_deployments.pop(done, None)

    future.add_done_callback(completed)
    return future


def _mark_background_deployment_observed(future: Future[None]) -> None:
    with _background_deployments_lock:
        _unobserved_background_deployments.pop(future, None)


def _shutdown_background_deploy_executor() -> None:
    """Drain decorator deployments before Python shuts down thread pools."""
    with _background_deploy_executor_lock:
        executor = _background_deploy_executor
    if executor is not None:
        executor.shutdown(wait=True)


def _report_unobserved_background_deployments() -> None:
    """Report failures from definition-only deploy scripts and fail the process.

    A script that only defines functions never touches a handle, so nothing
    would otherwise surface a failed deployment and the process would exit
    successfully on code that cannot serve.
    """
    with _background_deployments_lock:
        deployments = list(_unobserved_background_deployments.items())
        _unobserved_background_deployments.clear()

    failed = False
    for future, registered_name in deployments:
        if not future.done() or future.cancelled():
            continue
        error = future.exception()
        if error is None:
            continue
        failed = True
        print(
            f"Background deployment for function {registered_name!r} failed:",
            file=sys.stderr,
        )
        traceback.print_exception(error, file=sys.stderr)

    if not failed:
        return

    # An atexit callback cannot set the exit status the ordinary way: SystemExit
    # raised here is reported as "Exception ignored" and the process still exits
    # 0. os._exit can, at the cost of skipping atexit callbacks registered
    # before this module was imported, so flush what those would have flushed.
    # Callbacks run newest-first and this one is registered at import, so
    # anything registered later (the OTel provider shutdown in _tracing, say)
    # has already run.
    sys.stdout.flush()
    sys.stderr.flush()
    logging.shutdown()
    os._exit(1)


# ``ThreadPoolExecutor`` registers its own callback through this hook. Callbacks
# run newest-first, so registering after importing the executor lets deployments
# finish before concurrent.futures globally rejects nested executor submissions.
getattr(threading, "_register_atexit")(_shutdown_background_deploy_executor)
atexit.register(_report_unobserved_background_deployments)

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

    function_id: str | None = None
    """Stable identifier of the parent external function."""

    scaling_group_revision_id: str | None = None
    """Immutable scaling-group revision linked to this version."""

    created_at: datetime | None = None
    """Time at which this immutable version was created."""

    is_current: bool = False
    """Whether the parent external function currently serves this version."""


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


_USER_MODULE_NAME = "user_module"
_USER_MODULE_PATH = f"/app/{_USER_MODULE_NAME}.py"


def _generate_handler_shim(
    func_name: str,
    *,
    module_name: str = _USER_MODULE_NAME,
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
    module_name
        Module to import ``func_name`` from. ``user_module`` for single-file
        deploys; the function's real dotted module name for package deploys.
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
    from chalkcompute._arrow_serde import handler_runtime_source

    serde_runtime = handler_runtime_source()
    if batched:
        # Flatten all (caller_idx, row_idx, kwargs) across every caller so
        # each item is an independent unit of work.  Results are collected in
        # completion order (not submission order), then regrouped per caller.
        # This means a slow item in one caller never blocks another caller's
        # items from completing — true promise semantics.
        return (
            bootstrap
            + serde_runtime
            + textwrap.dedent(f"""\
            import inspect
            from concurrent.futures import ThreadPoolExecutor, as_completed

            from {module_name} import {func_name}

            _NO_ARGS_SENTINEL_COLUMN = {_NO_ARGS_SENTINEL_COLUMN!r}
            _ROW_METADATA_COLUMN = {_ROW_METADATA_COLUMN!r}
            _RESERVED_COLUMNS = (_NO_ARGS_SENTINEL_COLUMN, _ROW_METADATA_COLUMN)
            _target = inspect.unwrap({func_name})
            _is_coroutine = inspect.iscoroutinefunction(_target)
            _input_types = _chalk_typing.get_type_hints(
                _target, include_extras=True
            )
            _output_type = _chalk_result_annotation(_target)
            _input_decoders = {{
                name: _chalk_column_decoder(annotation)
                for name, annotation in _input_types.items()
                if name != "return"
            }}
            _output_encoder = _chalk_encoder(_output_type)


            def _flatten_events(events, contexts):
                # Each work item carries its own context and row metadata: items run
                # concurrently, on threads or as tasks, and a context variable set here
                # would not follow them. The context also differs per caller, since the
                # server coalesces several callers into one handler call.
                work = []
                for ci, event in enumerate(events):
                    context = contexts[ci] if ci < len(contexts) else None
                    row_metadata = event.get(_ROW_METADATA_COLUMN)
                    row_metadata = (
                        row_metadata.to_pylist() if row_metadata is not None else None
                    )
                    columns = {{
                        k: _input_decoders[k](v)
                        for k, v in event.items()
                        if k not in _RESERVED_COLUMNS
                    }}
                    n = len(next(iter(columns.values()))) if columns else 1
                    for ri in range(n):
                        work.append((
                            ci,
                            ri,
                            {{k: v[ri] for k, v in columns.items()}},
                            context,
                            row_metadata[ri] if row_metadata is not None else None,
                        ))
                return work


            def _regroup(work, flat_results, n_callers):
                bucket = {{}}
                for (ci, ri, _, _, _), val in zip(work, flat_results):
                    bucket.setdefault(ci, {{}})[ri] = val
                return [[bucket[ci][ri] for ri in sorted(bucket[ci])]
                        for ci in range(n_callers)]


            def _scope(context, row_metadata):
                # Returns None when chalkcompute is absent (strip-mode images), which
                # _unscope treats as "nothing was published".
                if not context and row_metadata is None:
                    return None
                try:
                    from chalkcompute._call_context import set_call_scope
                except ImportError:
                    return None

                return set_call_scope(context, row_metadata)


            def _unscope(token):
                if token is None:
                    return
                from chalkcompute._call_context import reset_call_scope

                reset_call_scope(token)


            def _call_one(kwargs, context, row_metadata):
                token = _scope(context, row_metadata)
                try:
                    return {func_name}(**kwargs)
                finally:
                    _unscope(token)


            async def _acall_one(kwargs, context, row_metadata):
                # asyncio.gather wraps each coroutine in a Task with its own copy of the
                # context, so this set is isolated to this row.
                token = _scope(context, row_metadata)
                try:
                    return await {func_name}(**kwargs)
                finally:
                    _unscope(token)


            def handler(events, contexts):
                contexts = contexts or []
                work = _flatten_events(events, contexts)
                if _is_coroutine:
                    import asyncio
                    async def _run_all():
                        coros = [_acall_one(kw, cx, rm) for _, _, kw, cx, rm in work]
                        raw = await asyncio.gather(*coros)
                        flat = (
                            raw
                            if _output_encoder is None
                            else [_output_encoder(value) for value in raw]
                        )
                        return _regroup(work, flat, len(events))
                    return asyncio.run(_run_all())
                bucket = {{}}
                with ThreadPoolExecutor(max_workers=max(1, len(work))) as ex:
                    futures = {{
                        ex.submit(_call_one, kw, cx, rm): (ci, ri)
                        for ci, ri, kw, cx, rm in work
                    }}
                    for fut in as_completed(futures):
                        ci, ri = futures[fut]
                        value = fut.result()
                        if _output_encoder is not None:
                            value = _output_encoder(value)
                        bucket.setdefault(ci, {{}})[ri] = value
                return [[bucket[ci][ri] for ri in sorted(bucket[ci])]
                        for ci in range(len(events))]
            """)
        )
    return (
        bootstrap
        + serde_runtime
        + textwrap.dedent(f"""\
        import contextlib
        import inspect

        from {module_name} import {func_name}

        _NO_ARGS_SENTINEL_COLUMN = {_NO_ARGS_SENTINEL_COLUMN!r}
        _ROW_METADATA_COLUMN = {_ROW_METADATA_COLUMN!r}
        _RESERVED_COLUMNS = (_NO_ARGS_SENTINEL_COLUMN, _ROW_METADATA_COLUMN)
        _target = inspect.unwrap({func_name})
        _is_sync_gen = inspect.isgeneratorfunction(_target)
        _is_async_gen = inspect.isasyncgenfunction(_target)
        _is_coroutine = inspect.iscoroutinefunction(_target) and not _is_async_gen
        _input_types = _chalk_typing.get_type_hints(
            _target, include_extras=True
        )
        _output_type = _chalk_result_annotation(_target)
        _input_decoders = {{
            name: _chalk_column_decoder(annotation)
            for name, annotation in _input_types.items()
            if name != "return"
        }}
        _output_encoder = _chalk_encoder(_output_type)


        def _kwargs_for_row(columns, row):
            return {{key: values[row] for key, values in columns.items()}}


        @contextlib.contextmanager
        def _call_scope(context, column, i):
            # Entered per row rather than once per call because a generator handler is
            # consumed after handler() returns, so a scope opened around the loop would
            # already be closed. Imported lazily so a function that reads neither the call
            # context nor row metadata does not need chalkcompute installed in its image.
            if not context and column is None:
                yield
                return
            try:
                from chalkcompute._call_context import (
                    reset_call_scope,
                    set_call_scope,
                )
            except ImportError:
                # Deploy images built in strip mode ship only the remote-call runtime, so
                # nothing in them can read these accessors either way.
                yield
                return

            token = set_call_scope(
                context, column[i].as_py() if column is not None else None
            )
            try:
                yield
            finally:
                reset_call_scope(token)


        def handler(event, context):
            row_metadata = event.get(_ROW_METADATA_COLUMN)
            columns = {{
                k: _input_decoders[k](v)
                for k, v in event.items()
                if k not in _RESERVED_COLUMNS
            }}
            n = len(next(iter(columns.values()))) if columns else 1
            if _is_sync_gen:
                def _gen():
                    for i in range(n):
                        kwargs = _kwargs_for_row(columns, i)
                        with _call_scope(context, row_metadata, i):
                            if _output_encoder is None:
                                yield from {func_name}(**kwargs)
                            else:
                                for item in {func_name}(**kwargs):
                                    yield _output_encoder(item)
                return _gen()
            elif _is_async_gen:
                async def _agen():
                    for i in range(n):
                        kwargs = _kwargs_for_row(columns, i)
                        with _call_scope(context, row_metadata, i):
                            async for item in {func_name}(**kwargs):
                                yield item if _output_encoder is None else _output_encoder(item)
                return _agen()
            elif _is_coroutine:
                import asyncio
                results = []
                for i in range(n):
                    kwargs = _kwargs_for_row(columns, i)
                    with _call_scope(context, row_metadata, i):
                        result = asyncio.run({func_name}(**kwargs))
                    if _output_encoder is not None:
                        result = _output_encoder(result)
                    results.append(result)
                return results
            else:
                results = []
                for i in range(n):
                    kwargs = _kwargs_for_row(columns, i)
                    with _call_scope(context, row_metadata, i):
                        result = {func_name}(**kwargs)
                    if _output_encoder is not None:
                        result = _output_encoder(result)
                    results.append(result)
                return results
        """)
    )


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


def _replica_count(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _scaling_group_is_ready(
    sg: sg_pb.ScalingGroupResponse | dict[str, Any],
    *,
    require_ready_replicas: bool = True,
) -> bool:
    if isinstance(sg, sg_pb.ScalingGroupResponse):
        status = sg.status
        ready_replicas = sg.ready_replicas
        available_replicas = sg.available_replicas
    else:
        status = sg.get("status", "")
        ready_replicas = _replica_count(sg.get("readyReplicas"))
        available_replicas = _replica_count(sg.get("availableReplicas"))
    if not require_ready_replicas:
        return status in ("Running", "Available", "ScaledToZero")
    if status not in ("Running", "Available"):
        return False
    return ready_replicas > 0 and available_replicas > 0


def _get_scaling_group(client: ConnectClient, name: str) -> sg_pb.ScalingGroupResponse:
    stub = client.stub(sg_connect.ScalingGroupManagerServiceClientSync)
    try:
        response = client.call(
            stub.get_scaling_group,
            sg_pb.GetScalingGroupRequest(name=name),
        )
    except ConnectRpcError as exc:
        raise FunctionError(f"GetScalingGroup failed: {exc}") from exc
    if response.scaling_group is None:
        raise FunctionError("GetScalingGroup returned no scaling group")
    return response.scaling_group


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
    from chalkcompute._remote_call_client import RemoteCallTransportError

    if isinstance(error, RemoteCallTransportError):
        return True
    if isinstance(error, ConnectRpcError) and error.code == Code.UNAVAILABLE:
        return True
    code_fn = getattr(error, "code", None)
    if callable(code_fn):
        try:
            code = code_fn()
            if getattr(code, "name", None) == "UNAVAILABLE":
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


def _available_module_name(base: str, taken: set[str]) -> str:
    """First of ``base``, ``base_2``, ``base_3``, ... not present in ``taken``."""
    name = base
    suffix = 2
    while name in taken:
        name = f"{base}_{suffix}"
        suffix += 1
    return name


def _package_dir_for_module(module_name: str) -> str | None:
    """Directory of the top-level package containing ``module_name``.

    Returns ``None`` for non-dotted module names (loose scripts,
    ``__main__``) and for packages that can't be resolved to a single
    directory — callers fall back to single-file deploy.
    """
    if "." not in module_name:
        return None
    package = sys.modules.get(module_name.split(".")[0])
    paths = list(getattr(package, "__path__", None) or [])
    if len(paths) != 1:
        return None
    return paths[0]


def _deploy_module_name(func: Callable[..., Any]) -> str:
    """Module name to deploy ``func`` from, seeing through module execution.

    ``uv run -m mypkg.mod`` executes the module as ``__main__`` but records
    its real dotted name and file on ``__main__.__spec__``. The ``samefile``
    check ensures the function really was defined in the module being run.
    Direct script runs have no spec and keep ``__main__``.
    """
    module_name = func.__module__ or ""
    if module_name != "__main__":
        return module_name
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    if spec is None or "." not in spec.name or not spec.origin:
        return module_name
    try:
        if os.path.samefile(spec.origin, _source_file_for_deploy(func)):
            return spec.name
    except (OSError, TypeError):
        pass
    return module_name


def _ancestor_package_names(source_file: str) -> set[str]:
    """Names of the packages ``source_file`` sits inside (nearest to top-most)."""
    packages: set[str] = set()
    directory = os.path.dirname(os.path.abspath(source_file))
    while os.path.exists(os.path.join(directory, "__init__.py")):
        packages.add(os.path.basename(directory))
        parent = os.path.dirname(directory)
        if parent == directory:  # filesystem root: stop, never loop
            break
        directory = parent
    return packages


def _check_direct_run_from_package(func: Callable[..., Any], func_name: str) -> None:
    """Raise when a directly-run package file imports its own package.

    The file executed as ``__main__`` with no module spec, so its package
    name is unrecoverable and the package cannot ship — any import of an
    ancestor package is guaranteed to fail remotely. Files that don't
    import their own package (self-contained, or flat imports declared via
    ``add_local_python_source``) deploy fine as a single file, so they
    pass silently.
    """
    source_file = _source_file_for_deploy(func)
    packages = _ancestor_package_names(source_file)
    if not packages:
        return
    try:
        tree = ast.parse(_source_text_for_deploy(source_file))
    except (FunctionBuildError, SyntaxError):
        # Unreadable or unparsable source: skip the check rather than block;
        # a real read failure resurfaces when the deploy ships the file.
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            if name.split(".")[0] in packages:
                raise FunctionError(
                    f"Function {func_name!r} is defined in {source_file}, which "
                    f"imports its own package via {name!r}, but the file was run "
                    f"directly, so the package cannot be shipped and this import "
                    f"would fail remotely. Run it with 'python -m <pkg>.<module>' "
                    f"or import it from a separate entrypoint to ship the whole "
                    f"package."
                )


def _strip_chalkcompute_for_package(source: str) -> str:
    from chalkcompute._cls import _strip_chalkcompute_from_class_source

    return _strip_chalkcompute_from_class_source(
        _strip_chalkcompute_from_source(source)
    )


def _prepare_package_dir(
    package_dir: str,
    *,
    label: str,
    entrypoint_file: str,
    entrypoint_sources: typing.Sequence[str],
) -> tuple[str, bool]:
    """Return the package directory to deploy, stripping chalkcompute if needed.

    In strip mode every ``.py`` file in the package is rewritten (both the
    function and class strip passes, since any file may use either
    decorator) into a temp copy of the tree.

    Parameters
    ----------
    package_dir
        Directory of the top-level package to deploy.
    label
        Human-readable identifier used in the strip-mode guard error.
    entrypoint_file
        Source file containing the deployed function/class, checked by the
        strip-mode guard.
    entrypoint_sources
        Source of the deployed function(s)/method(s) whose bodies the
        strip-mode guard inspects.

    Returns
    -------
    tuple of (str, bool)
        ``(path, is_temp)`` — if stripping produced a temp directory, the
        caller should clean it up.
    """
    if not _should_strip_source():
        return package_dir, False

    original = _source_text_for_deploy(entrypoint_file)
    _strip_source_for_deploy(
        original,
        strip=_strip_chalkcompute_for_package,
        label=label,
        entrypoint_sources=entrypoint_sources,
    )

    temp_dir = tempfile.mkdtemp(prefix="chalkfn_pkg_")
    shutil.copytree(
        package_dir,
        temp_dir,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        dirs_exist_ok=True,
    )
    for py_file in Path(temp_dir).rglob("*.py"):
        py_file.write_text(_strip_chalkcompute_for_package(py_file.read_text()))
    return temp_dir, True


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


def _loaded_names_in_annotations(entrypoint_sources: typing.Iterable[str]) -> set[str]:
    """Bare names read in the *signature* of the given defs — parameters and return.

    Annotations are not statements in the body, so
    :func:`_loaded_names_in_bodies` never sees them. They still resolve at call
    time: the generated worker shim calls ``typing.get_type_hints`` on the
    deployed target, which evaluates every annotation in the stripped module's
    namespace. An annotation like ``trace: cc.Trace`` therefore raises
    ``NameError`` in the container once ``import chalkcompute as cc`` is gone,
    even though nothing in the body mentions ``cc``.
    """
    used: set[str] = set()
    for src in entrypoint_sources:
        try:
            tree = ast.parse(textwrap.dedent(src))
        except (SyntaxError, IndentationError):
            continue
        for top in tree.body:
            if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = top.args
            annotations: list[ast.expr] = [top.returns] if top.returns else []
            for arg in (
                *args.posonlyargs,
                *args.args,
                *args.kwonlyargs,
                args.vararg,
                args.kwarg,
            ):
                if arg is not None and arg.annotation is not None:
                    annotations.append(arg.annotation)
            for annotation in annotations:
                for node in ast.walk(annotation):
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
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

    Both the bodies and the signatures in ``entrypoint_sources`` (the deployed
    function / class methods) are inspected; unrelated functions in the same
    file are ignored. Annotations count because the worker shim resolves them
    with ``typing.get_type_hints`` — see
    :func:`_loaded_names_in_annotations`.
    """
    if not entrypoint_sources:
        return
    bound_before = _chalkcompute_bound_names(original_source)
    bound_after = _chalkcompute_bound_names(stripped_source)
    used = _loaded_names_in_bodies(entrypoint_sources) | _loaded_names_in_annotations(
        entrypoint_sources
    )
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
        f"        ...\n"
        f"An inner import does not rescue a *parameter annotation* such as "
        f"'trace: chalkcompute.Trace' — the worker shim resolves annotations with "
        f"typing.get_type_hints before the body runs. Annotate the wire type "
        f"instead ('trace: str') and rebuild the handle inside the body, or "
        f"install chalkcompute in the image with strip mode off."
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
    stripped = _strip_source_for_deploy(
        source,
        strip=_strip_chalkcompute_from_source,
        label=label or f"Function source {source_file!r}",
        entrypoint_sources=entrypoint_sources or [],
    )
    return _write_temp(stripped), True


def _strip_source_for_deploy(
    source: str,
    *,
    strip: Callable[[str], str],
    label: str,
    entrypoint_sources: typing.Sequence[str],
) -> str:
    """Strip chalkcompute from ``source`` and assert the result is usable.

    Stripping and validating are paired here so no deploy surface can do one
    without the other. Callers pass the strip pass their deploy mode uses:
    ``_strip_chalkcompute_from_source`` for a single function file,
    ``_strip_chalkcompute_for_package`` for a package (function + class passes),
    ``_strip_chalkcompute_from_class_source`` for a ``@cls`` file. The passes
    remove different name sets, so a mismatch would let a real violation
    through.

    Parameters
    ----------
    source
        Original source text of the file being deployed.
    strip
        Strip pass matching the caller's deploy mode.
    label
        Human-readable identifier used in the guard's error message.
    entrypoint_sources
        Source of the deployed function(s)/method(s) whose bodies the guard
        inspects for dangling chalkcompute references.

    Returns
    -------
    str
        The stripped source.
    """
    stripped = strip(source)
    _assert_stripped_source_self_contained(
        source,
        stripped,
        label=label,
        entrypoint_sources=list(entrypoint_sources),
    )
    return stripped


def _assert_strip_mode_self_contained(func: Callable[..., Any], *, label: str) -> None:
    """Run the strip-mode guard without building anything.

    The guard is a pure function of the source text and
    ``CHALK_FUNCTION_STRIP_SOURCE``, so it can run before a deploy is even
    scheduled. Callers use this to keep a strip-mode violation a synchronous
    error on the defining thread rather than a background-thread failure that a
    definition-only script only reports at interpreter exit — by which point the
    process has already exited successfully.

    ``@cls`` needs no equivalent: its decorator deploys synchronously, so its
    guard already raises on the defining thread.

    Parameters
    ----------
    func
        The undecorated user function about to be deployed.
    label
        Human-readable identifier used in the guard's error message.
    """
    if not _should_strip_source():
        return

    package_dir = _package_dir_for_module(_deploy_module_name(func))
    _strip_source_for_deploy(
        _source_text_for_deploy(_source_file_for_deploy(func)),
        strip=(
            _strip_chalkcompute_for_package
            if package_dir is not None
            else _strip_chalkcompute_from_source
        ),
        label=label,
        entrypoint_sources=_entrypoint_source(func),
    )


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
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version

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


def _cleanup_temp_files(paths: list[str]) -> None:
    """Remove temporary files or directories, ignoring errors.

    Parameters
    ----------
    paths
        Paths to temporary files/directories to remove.
    """
    for tmp in paths:
        try:
            if os.path.isdir(tmp):
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def _build_schemas(func: Callable[..., Any]) -> tuple[Any, Any]:
    """Derive input/output ``arrow_pb.Schema`` protos from function type hints.

    Goes directly from Python type annotations to ``arrow_pb.Schema`` protos
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

    hints = typing.get_type_hints(func, include_extras=True)
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


def _extract_field_names(schema: Any | None) -> list[str]:
    """Extract field names from a typed Arrow schema.

    Parameters
    ----------
    schema
        Arrow schema protobuf, legacy JSON dict, or ``None``.

    Returns
    -------
    list of str
        Field names in declaration order.
    """
    if schema is None:
        return []
    if hasattr(schema, "columns"):
        return [field.name for field in schema.columns]
    return [f["name"] for f in schema.get("columns", []) if "name" in f]


def _extract_schema_metadata(schema: Any | None) -> dict[str, str]:
    """Return Arrow schema metadata from a typed or legacy JSON schema."""
    if schema is None:
        return {}
    if hasattr(schema, "metadata"):
        return dict(schema.metadata)
    metadata = schema.get("metadata") or {}
    if isinstance(metadata, dict):
        return {str(k): str(v) for k, v in metadata.items()}
    if isinstance(metadata, list):
        result: dict[str, str] = {}
        for entry in metadata:
            if isinstance(entry, dict) and "key" in entry:
                result[str(entry["key"])] = str(entry.get("value", ""))
        return result
    return {}


def _extract_function_traits(
    efv: catalog_pb.ExternalFunctionVersion | dict[str, Any],
) -> tuple[bool, bool]:
    """Return ``(is_generator, is_async)`` from catalog function metadata."""
    metadata = _extract_schema_metadata(
        efv.output_arrow_schema
        if isinstance(efv, catalog_pb.ExternalFunctionVersion)
        else efv.get("outputArrowSchema")
    )
    return (
        metadata.get("chalk.function.is_generator") == "true",
        metadata.get("chalk.function.is_async") == "true",
    )


def _parse_function_version(
    efv: catalog_pb.ExternalFunctionVersion,
    *,
    is_current: bool,
) -> FunctionVersionInfo:
    return FunctionVersionInfo(
        id=efv.id,
        function_id=efv.function_id or None,
        function_name=efv.function_name,
        version=efv.version,
        scaling_group_name=efv.scaling_group_name,
        scaling_group_revision_id=efv.scaling_group_revision_id or None,
        created_at=None if efv.created_at is None else efv.created_at.to_datetime(),
        is_current=is_current,
    )


def _apply_function_version(
    function: RemoteFunction,
    efv: catalog_pb.ExternalFunctionVersion,
    *,
    is_current: bool,
) -> None:
    function._function_id = efv.function_id or None
    function._version_info = _parse_function_version(efv, is_current=is_current)
    # A locally defined handle keeps its Python name, signature (including
    # defaults), and Arrow serializers. Catalog names need not be valid Python
    # identifiers, and replacing the callable name changes the next deploy's
    # handler source. Only attached handles derive callable metadata from the API.
    if function._has_local_func:
        return
    function._func_name = efv.function_name
    function._explicit_name = sanitize_dns_label(efv.function_name)
    function._input_field_names = _extract_field_names(efv.input_arrow_schema)
    function._call_parameter_names, function._bind_call_arguments = (
        _compile_catalog_argument_binder(function._input_field_names)
    )
    function._arrow_input_serializers = (None,) * len(function._call_parameter_names)
    function._is_generator, function._is_async = _extract_function_traits(efv)


def _apply_external_function(
    function: RemoteFunction,
    external_function: catalog_pb.ExternalFunction | None,
    *,
    scaling_group: sg_pb.ScalingGroupResponse | None = None,
) -> None:
    if external_function is None:
        raise FunctionError("External-function response contained no function")
    if external_function.current_version is None:
        raise FunctionError("External function has no selected version")
    _apply_function_version(
        function,
        external_function.current_version,
        is_current=True,
    )
    function._function_id = external_function.id
    function._web_url = None if scaling_group is None else scaling_group.web_url or None
    function._port = 6666


def _get_external_function(
    client: ConnectClient,
    *,
    function_id: str | None = None,
    function_name: str | None = None,
    include_scaling_group: bool = False,
) -> catalog_pb.GetExternalFunctionResponse:
    stub = client.stub(catalog_connect.ExternalFunctionCatalogServiceClientSync)
    response = client.call(
        stub.get_external_function,
        catalog_pb.GetExternalFunctionRequest(
            function_id=function_id,
            function_name=function_name,
            include_scaling_group=include_scaling_group,
        ),
    )
    if response.external_function is None:
        raise FunctionError("GetExternalFunction returned no function")
    return response


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


_MISSING_ARGUMENT = object()
_ArgumentBinder = Callable[[tuple[Any, ...], dict[str, Any]], tuple[Any, ...]]


def _compile_fixed_argument_binder(
    names: tuple[str, ...],
    *,
    positional_count: int,
    defaults: tuple[Any, ...],
) -> _ArgumentBinder:
    """Compile argument binding for a fixed, non-variadic parameter list."""
    indexes = {name: index for index, name in enumerate(names)}
    parameter_count = len(names)

    def bind(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Any, ...]:
        positional_given = len(args)
        if positional_given > positional_count:
            raise TypeError(
                f"Expected at most {positional_count} positional arguments, "
                f"got {positional_given}"
            )

        if not kwargs and positional_given == parameter_count:
            return args

        values = list(defaults)
        values[:positional_given] = args
        for name, value in kwargs.items():
            index = indexes.get(name)
            if index is None:
                raise TypeError(f"Unexpected keyword argument: {name!r}")
            if index < positional_given:
                raise TypeError(f"Multiple values for argument: {name!r}")
            values[index] = value

        for index, value in enumerate(values):
            if value is _MISSING_ARGUMENT:
                raise TypeError(f"Missing argument: {names[index]}")
        return tuple(values)

    return bind


def _compile_argument_binder(
    signature: inspect.Signature,
) -> tuple[tuple[str, ...], _ArgumentBinder]:
    """Compile the common fixed-signature path; retain bind() as a fallback."""
    parameters = tuple(signature.parameters.values())
    value_parameters = tuple(
        parameter
        for parameter in parameters
        if parameter.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    )
    names = tuple(parameter.name for parameter in value_parameters)

    if any(
        parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        )
        for parameter in parameters
    ):

        def bind_fallback(
            args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> tuple[Any, ...]:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            return tuple(bound.arguments[name] for name in names)

        return names, bind_fallback

    positional_count = sum(
        parameter.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in value_parameters
    )
    defaults = tuple(
        _MISSING_ARGUMENT
        if parameter.default is inspect.Parameter.empty
        else parameter.default
        for parameter in value_parameters
    )
    return names, _compile_fixed_argument_binder(
        names,
        positional_count=positional_count,
        defaults=defaults,
    )


def _compile_catalog_argument_binder(
    names: typing.Sequence[str],
) -> tuple[tuple[str, ...], _ArgumentBinder]:
    """Compile binding for an attached function whose catalog exposes names only."""
    names_tuple = tuple(names)
    return names_tuple, _compile_fixed_argument_binder(
        names_tuple,
        positional_count=len(names_tuple),
        defaults=(_MISSING_ARGUMENT,) * len(names_tuple),
    )


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
        volumes: list[tuple[str, str] | Volume | VolumeMount] | None = None,
        tags: dict[str, str] | None = None,
        chalk_identity: bool = False,
        authenticated: bool = False,
        readiness_probe: _ReadinessProbe | None = None,
        min_replicas: int = 1,
        max_replicas: int = 1,
        shutdown_delay_seconds: int | None = None,
        scaling_interval_seconds: int | None = None,
        target_cpu_utilization_percentage: int | None = None,
        target_gpu_utilization_percentage: int | None = None,
        target_queue_depth: int | None = None,
        cron_scaling_trigger: _CronScalingTrigger | None = None,
        scale_from_zero_request_policy: _ScaleFromZeroRequestPolicy | None = None,
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
        self._call_parameter_names, self._bind_call_arguments = (
            _compile_argument_binder(inspect.signature(func))
        )

        from chalkcompute._arrow_serde import (
            arrow_decoder_for_annotation,
            arrow_encoder_for_annotation,
            unwrap_result_annotation,
        )

        hints = typing.get_type_hints(func, include_extras=True)
        serializers_by_name: dict[str, Callable[[Any], Any]] = {}
        for param_name, annotation in hints.items():
            if param_name == "return":
                continue
            serializer = arrow_encoder_for_annotation(annotation)
            if serializer is not None:
                serializers_by_name[param_name] = serializer
        self._arrow_input_serializers: tuple[Callable[[Any], Any] | None, ...] = tuple(
            serializers_by_name.get(param_name)
            for param_name in self._call_parameter_names
        )
        result_type = unwrap_result_annotation(hints.get("return"))
        self._arrow_result_deserializer: Callable[[Any], Any] | None = (
            arrow_decoder_for_annotation(result_type)
        )
        ContainerSpec.validate_fields(
            image=image,
            name=name,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            env=env,
            secrets=secrets,
            tags=tags,
            chalk_identity=chalk_identity,
        )
        validate_replica_and_utilization(
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            gpu=gpu,
            target_cpu_utilization_percentage=target_cpu_utilization_percentage,
            target_gpu_utilization_percentage=target_gpu_utilization_percentage,
        )
        self._explicit_name = name
        self._image = image
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        self._env = dict(env) if env else {}
        if models:
            from chalkcompute._model import MODELS_ENV_VAR, encode_models_env

            self._env[MODELS_ENV_VAR] = encode_models_env(models)
        self._secrets: list[_Secret] = list(secrets or [])
        # Existence is checked at deploy time, not here.
        self._referenced_volumes: list[VolumeMount] = normalize_volume_refs(volumes)
        # Populated at deploy time: referenced volumes plus any freshly
        # uploaded volumes.
        self._volumes: list[VolumeMount] = []
        self._tags = dict(tags or {})
        self._chalk_identity = chalk_identity
        self._authenticated = authenticated
        if readiness_probe is not None:
            from chalkcompute._scaling_group import ReadinessProbe

            if not isinstance(readiness_probe, ReadinessProbe):
                raise TypeError("readiness_probe must be a ReadinessProbe")
        self._readiness_probe = readiness_probe
        if scale_from_zero_request_policy is not None:
            from chalkcompute._scaling_group import ScaleFromZeroRequestPolicy

            if not isinstance(
                scale_from_zero_request_policy, ScaleFromZeroRequestPolicy
            ):
                raise TypeError(
                    "scale_from_zero_request_policy must be a ScaleFromZeroRequestPolicy"
                )
        self._scale_from_zero_request_policy = scale_from_zero_request_policy
        self._volume_driver: VolumeClient | None = None
        self._min_replicas = min_replicas
        self._max_replicas = max_replicas
        self._shutdown_delay_seconds = shutdown_delay_seconds
        self._scaling_interval_seconds = scaling_interval_seconds
        self._target_cpu_utilization_percentage = target_cpu_utilization_percentage
        self._target_gpu_utilization_percentage = target_gpu_utilization_percentage
        self._target_queue_depth = target_queue_depth
        self._cron_scaling_trigger = cron_scaling_trigger
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
        self._function_id: str | None = None
        self._version_info: FunctionVersionInfo | None = None
        self._web_url: str | None = None
        self._port: int = 8080
        self._temp_files: list[str] = []
        self._handler_module: str = "handler"

        # Input field names (populated from API response for from_id/from_name)
        self._input_field_names: list[str] = []
        self._is_generator = inspect.isgeneratorfunction(
            func
        ) or inspect.isasyncgenfunction(func)
        self._is_async = inspect.iscoroutinefunction(
            func
        ) or inspect.isasyncgenfunction(func)

        # ConnectRPC fallback client (lazily created on first call if needed).
        self._call_stub: Any = None
        self._remote_call_client: Any = None
        self._queue_call_client: Any = None
        self._remote_call_auth_token: str | None = None
        self._queue_call_auth_token: str | None = None
        self._knowledge_cutoff: str | None = None
        self._background_deploy_future: Future[None] | None = None

        self._client = ConnectClient(chalk_client=chalk_client)

        functools.update_wrapper(self, func)
        _preserve_functionlike_metadata(self, func)

    # -- Alternate constructors ------------------------------------------------

    @classmethod
    def from_id(
        cls, version_id: str, *, chalk_client: Any | None = None
    ) -> RemoteFunction:
        """Attach to an existing function version by ID.

        This compatibility alias retains the historical version-ID meaning.
        Prefer :meth:`from_version_id` or :meth:`from_function_id` in new code.
        """
        return cls.from_version_id(version_id, chalk_client=chalk_client)

    @classmethod
    def from_version_id(
        cls, version_id: str, *, chalk_client: Any | None = None
    ) -> RemoteFunction:
        """Attach through an immutable version ID.

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
        stub = f._client.stub(catalog_connect.ExternalFunctionCatalogServiceClientSync)
        response = f._client.call(
            stub.get_external_function_version,
            catalog_pb.GetExternalFunctionVersionRequest(
                identifier=Oneof("id", version_id),
                include_scaling_group=True,
            ),
        )
        efv = response.external_function_version
        if efv is None:
            raise FunctionError("GetExternalFunctionVersion returned no version")
        _apply_function_version(f, efv, is_current=False)
        if response.scaling_group is not None:
            f._web_url = response.scaling_group.web_url or None
            f._port = 6666
        return f

    @classmethod
    def from_name(cls, name: str, *, chalk_client: Any | None = None) -> RemoteFunction:
        """Attach to an external function's currently selected version.

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
        response = _get_external_function(
            f._client,
            function_name=name,
            include_scaling_group=True,
        )
        _apply_external_function(
            f,
            response.external_function,
            scaling_group=response.scaling_group,
        )
        return f

    @classmethod
    def from_function_id(
        cls, function_id: str, *, chalk_client: Any | None = None
    ) -> RemoteFunction:
        """Attach to a stable external function by ID."""
        f = cls._make_empty(chalk_client=chalk_client)
        response = _get_external_function(
            f._client,
            function_id=function_id,
            include_scaling_group=True,
        )
        _apply_external_function(
            f,
            response.external_function,
            scaling_group=response.scaling_group,
        )
        return f

    @classmethod
    def _make_empty(cls, *, chalk_client: Any | None = None) -> RemoteFunction:
        f = cls.__new__(cls)
        f._func = lambda *a, **kw: None  # type: ignore[assignment]
        f._has_local_func = False
        f._func_name = "unknown"
        f._call_parameter_names, f._bind_call_arguments = (
            _compile_catalog_argument_binder(())
        )
        f._explicit_name = "unknown"
        f._image = None
        f._cpu = None
        f._memory = None
        f._gpu = None
        f._env = {}
        f._secrets = []
        f._referenced_volumes = []
        f._volumes = []
        f._tags = {}
        f._chalk_identity = False
        f._authenticated = False
        f._readiness_probe = None
        f._scale_from_zero_request_policy = None
        f._volume_driver = None
        f._min_replicas = 1
        f._max_replicas = 1
        f._shutdown_delay_seconds = None
        f._scaling_interval_seconds = None
        f._target_cpu_utilization_percentage = None
        f._target_gpu_utilization_percentage = None
        f._target_queue_depth = None
        f._cron_scaling_trigger = None
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
        f._function_id = None
        f._version_info = None
        f._web_url = None
        f._port = 6666
        f._input_field_names = []
        f._arrow_input_serializers = ()
        f._arrow_result_deserializer = None
        f._is_generator = False
        f._is_async = False
        f._call_stub = None
        f._remote_call_client = None
        f._queue_call_client = None
        f._remote_call_auth_token = None
        f._queue_call_auth_token = None
        f._knowledge_cutoff = None
        f._background_deploy_future = None
        f._client = ConnectClient(chalk_client=chalk_client)
        return f

    # -- Image building --------------------------------------------------------

    def _add_package_source(
        self,
        base_image: _Image,
        package_dir: str,
        module_name: str,
        temp_files: list[str],
    ) -> _Image:
        """Add the function's whole top-level package to the image."""
        prepared_dir, dir_is_temp = _prepare_package_dir(
            package_dir,
            label=f"Function {self._func_name!r}",
            entrypoint_file=_source_file_for_deploy(self._func),
            entrypoint_sources=_entrypoint_source(self._func),
        )
        if dir_is_temp:
            temp_files.append(prepared_dir)
        return base_image.add_local_dir(
            prepared_dir,
            f"/app/{module_name.split('.')[0]}",
            exclude=["**/__pycache__/**", "**/*.pyc"],
        )

    def _add_single_file_source(
        self,
        base_image: _Image,
        temp_files: list[str],
        module_name: str,
    ) -> _Image:
        """Add the function's source file to the image as ``<module_name>.py``."""
        source_file, source_is_temp = _prepare_source_file(
            _source_file_for_deploy(self._func),
            label=f"Function {self._func_name!r}",
            entrypoint_sources=_entrypoint_source(self._func),
        )
        if source_is_temp:
            temp_files.append(source_file)
        return base_image.add_local_file(source_file, f"/app/{module_name}.py")

    def _add_handler_shim(
        self,
        deploy_image: _Image,
        shim_module: str,
        temp_files: list[str],
        handler_module: str,
    ) -> _Image:
        """Generate the handler shim and finalize the deploy image env."""
        from chalkcompute._model import MODELS_ENV_VAR

        handler_shim = _generate_handler_shim(
            self._func_name,
            module_name=shim_module,
            batched=self._max_batching_size is not None,
            bind_models=bool(self._env.get(MODELS_ENV_VAR)),
        )
        shim_path = _write_temp(handler_shim)
        temp_files.append(shim_path)

        return deploy_image.add_local_file(shim_path, f"/app/{handler_module}.py").env(
            {"PYTHONPATH": "/app", _REMOTE_ENV_VAR: "1"}
        )

    def _build_deploy_image(self) -> _Image:
        """Build the deploy image with handler shim and user code.

        When the function is defined in a package (its module name is
        dotted), the whole top-level package is shipped and the shim imports
        the function from its real module, so sibling-module helpers resolve
        remotely. This includes running the module via ``uv run -m
        mypkg.mod``: the real dotted name is recovered from
        ``__main__.__spec__``. Otherwise the single source file is shipped
        as ``user_module.py``.

        The shim (and, in single-file mode, the renamed source module) get
        generated names that dodge any top-level ``/app`` name the user's
        image already claims, so user modules named ``handler`` or
        ``user_module`` never collide with the SDK's artifacts.

        Returns
        -------
        Image
            Declarative image ready to be built.
        """
        module_name = _deploy_module_name(self._func)
        package_dir = _package_dir_for_module(module_name)

        base_image = _make_base_image(
            self._image,
            remote_call_runtime_package=self._remote_call_runtime_package(),
        )
        taken = base_image._app_module_names()

        temp_files: list[str] = []
        if package_dir is not None:
            # Function lives in a package: ship the whole top-level package.
            taken.add(module_name.split(".")[0])
            shim_module = module_name
            deploy_image = self._add_package_source(
                base_image, package_dir, module_name, temp_files
            )
        elif module_name == "__main__":
            # File run directly: error if it imports its own package, else ship one file.
            _check_direct_run_from_package(self._func, self._func_name)
            shim_module = _available_module_name(_USER_MODULE_NAME, taken)
            taken.add(shim_module)
            deploy_image = self._add_single_file_source(
                base_image, temp_files, shim_module
            )
        else:
            # Loose non-package module: ship its single source file.
            shim_module = _available_module_name(_USER_MODULE_NAME, taken)
            taken.add(shim_module)
            deploy_image = self._add_single_file_source(
                base_image, temp_files, shim_module
            )

        self._handler_module = _available_module_name("handler", taken)
        deploy_image = self._add_handler_shim(
            deploy_image, shim_module, temp_files, self._handler_module
        )

        self._temp_files = temp_files
        return deploy_image

    # -- Lifecycle -------------------------------------------------------------

    def _start_background_deploy(self) -> None:
        if self._background_deploy_future is not None:
            raise FunctionError(
                f"Background deployment already started for {self._func_name!r}"
            )
        _assert_strip_mode_self_contained(
            self._func, label=f"Function {self._func_name!r}"
        )
        self._background_deploy_future = _schedule_background_deployment(self)

    def _wait_for_background_deploy(self, timeout: float | None = None) -> bool:
        """Wait for the decorator-triggered deployment, if this handle has one."""
        future = self._background_deploy_future
        if future is None:
            return False
        try:
            future.result(timeout=timeout)
        except TimeoutError as exc:
            if not future.done():
                raise FunctionError(
                    f"Function {self._func_name!r} is still deploying after {timeout}s"
                ) from exc
            _mark_background_deployment_observed(future)
            raise
        except BaseException:
            if future.done():
                _mark_background_deployment_observed(future)
            raise
        _mark_background_deployment_observed(future)
        return True

    def _settle_background_deploy(self) -> None:
        """Wait for the decorator's deployment, then release this handle from it.

        Used by :meth:`deploy`, which is a caller-initiated retry: it must
        serialize behind the decorator's attempt so both do not select versions
        of the same registered name concurrently, but it must not inherit that
        attempt's failure. A stored exception would otherwise be re-raised on
        every later ``.deploy()`` — the future is never reset — leaving the
        handle permanently unable to retry.

        The future is cleared either way, so a successful retry also leaves a
        clean handle instead of one whose ``.remote()`` still re-raises the old
        build error.
        """
        future = self._background_deploy_future
        if future is None:
            return
        try:
            future.result()
        except Exception as exc:
            logger.warning(
                "Background deployment for function '%s' failed (%s); "
                "retrying with an explicit deploy.",
                self._explicit_name or self._func_name,
                exc,
            )
        finally:
            _mark_background_deployment_observed(future)
            self._background_deploy_future = None

    def deploy(
        self,
        *,
        poll_interval: float = 0.5,
        build_timeout: float = 600.0,
        ready_timeout: float = 300.0,
        force_new_version: bool = False,
    ) -> RemoteFunction:
        """Ensure the definition is deployed, reusing an unchanged version.

        Parameters
        ----------
        poll_interval
            Seconds between status polls.
        build_timeout
            Maximum seconds to wait for the image build.
        ready_timeout
            Maximum seconds to wait for the scaling group to become ready.
        force_new_version
            Create a new version even when unchanged, reusing image/source artifacts.

        Returns
        -------
        RemoteFunction
            ``self`` for chaining.
        """
        self._settle_background_deploy()
        with _manual_deploy_slot(self._explicit_name or self._func_name):
            return self._deploy(
                poll_interval=poll_interval,
                build_timeout=build_timeout,
                ready_timeout=ready_timeout,
                force_new_version=force_new_version,
            )

    @_instrumented("chalkcompute.function.deploy")
    def _deploy(
        self,
        *,
        poll_interval: float = 0.5,
        build_timeout: float = 600.0,
        ready_timeout: float = 300.0,
        force_new_version: bool = False,
    ) -> RemoteFunction:
        """Build the image, register the function version, and wait until running.

        This is the deploy body without the background-deployment barrier, so
        the decorator's own worker can call it without waiting on itself.
        Callers other than that worker should use :meth:`deploy`.

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
        from chalkcompute._progress import (
            progress_add,
            progress_done,
            progress_fail,
            progress_finish,
            progress_update,
        )

        registered_name = self._explicit_name or self._func_name
        fkey = f"function:{registered_name}"
        progress_add(fkey, "function", f'Function "{self._func_name}"')

        # Fail fast on the hostname-length limit before the expensive build.
        # Uses whatever env_id is already resolved (skips if unauthenticated;
        # the server enforces the limit regardless) — never forces auth here.
        sg_name = sanitize_dns_label(self._explicit_name or self._func_name)
        validate_dns_label(sg_name, self._client._env_id)

        try:
            # 1. Build deploy image
            deploy_image = self._build_deploy_image()

            prepared = prepare_function_image(deploy_image)
            deploy_volumes = list(getattr(self, "_referenced_volumes", []))
            self._volumes = deploy_volumes
            self._volume_driver = None

            # 3. Register function version
            input_schema_proto, output_schema_proto = _build_schemas(self._func)

            handler_cmd = [
                "chalk-remote-call",
                "--handler",
                f"{self._handler_module}.handler",
            ]

            resolved_secrets, local_secrets = prepare_lazy_secrets(self._secrets)

            container_spec = ContainerSpec(
                name=sg_name,
                image="",
                entrypoint=_local_install_entrypoint(handler_cmd),
                env=_remote_call_container_env(self._env),
                cpu=self._cpu,
                memory=self._memory,
                gpu=self._gpu,
                volumes=deploy_volumes,
                secret_refs=resolved_secrets,
                port=6666,
                protocol="grpc",
                startup_probe=_remote_function_startup_probe(),
                readiness_probe=(
                    self._readiness_probe.to_dict()
                    if self._readiness_probe is not None
                    else None
                ),
                authentication="AUTHENTICATED" if self._authenticated else None,
                tags=self._tags,
                chalk_identity=self._chalk_identity,
            ).to_proto()
            spec = sg_pb.ScalingGroupSpec(
                container_spec=container_spec,
                scaling_spec=scaling_spec(
                    min_replicas=self._min_replicas,
                    max_replicas=self._max_replicas,
                    shutdown_delay_seconds=self._shutdown_delay_seconds,
                    window_seconds=self._scaling_interval_seconds,
                    target_cpu_utilization_percentage=(
                        self._target_cpu_utilization_percentage
                    ),
                    target_gpu_utilization_percentage=(
                        self._target_gpu_utilization_percentage
                    ),
                    function_name=self._explicit_name or self._func_name,
                    target_queue_depth=self._target_queue_depth,
                    cron_scaling_trigger=self._cron_scaling_trigger,
                    scale_from_zero_request_policy=self._scale_from_zero_request_policy,
                ),
            )

            # 5b. Build function-level config
            config = function_config(
                serialization_format=self._serialization_format,
                options=self._options,
                max_buffer_duration=self._max_buffer_duration,
                max_batching_size=self._max_batching_size,
                retries=self._retries,
                rate_limit=self._rate_limit,
                concurrency=self._concurrency,
                quota=self._quota,
                schedule=self._schedule,
                tracing=self._tracing,
            )

            # 6. Create the stable function or append and select a new version.
            rkey = f"register:{registered_name}"
            progress_add(rkey, "function", "Registering function", parent=fkey)
            logger.info(
                "Registering function '%s'...", self._explicit_name or self._func_name
            )

            ensured = ensure_external_function(
                self._client,
                function_name=self._explicit_name or self._func_name,
                prepared=prepared,
                local_secrets=local_secrets,
                force_new_version=force_new_version,
                poll_interval=poll_interval,
                timeout=build_timeout,
                version_spec=catalog_pb.ExternalFunctionVersionSpec(
                    input_arrow_schema=input_schema_proto,
                    output_arrow_schema=output_schema_proto,
                    spec=spec,
                    config=config,
                ),
            )
            _apply_external_function(
                self, ensured.external_function, scaling_group=ensured.scaling_group
            )
            # A redeploy may move the stable function to a different scaling
            # group endpoint. Force subsequent calls to establish fresh
            # transports for the newly selected version.
            self._drop_remote_call_clients()
            assert self._version_info is not None
            sg_data = ensured.scaling_group

            logger.info(
                "Function version selected: %s v%s (id=%s)",
                self._version_info.function_name,
                self._version_info.version,
                self._version_info.id,
            )
            progress_done(rkey)

            # 7. Poll until the backing pod is actually ready to serve.
            if not (
                sg_data
                and _scaling_group_is_ready(
                    sg_data,
                    require_ready_replicas=self._min_replicas > 0,
                )
            ):
                progress_update(fkey, detail="waiting for ready")
                self._poll_until_ready(
                    sg_name=self._version_info.scaling_group_name,
                    poll_interval=poll_interval,
                    timeout=ready_timeout,
                    require_ready_replicas=self._min_replicas > 0,
                )

            progress_done(fkey, detail="ready")
        except BaseException:
            progress_fail(fkey)
            raise
        finally:
            _cleanup_temp_files(getattr(self, "_temp_files", []))
            self._temp_files = []
            progress_finish()

        return self

    def _remote_call_runtime_package(self) -> str:
        extras: list[str] = []
        if isinstance(self._tracing, TracingPolicy):
            extras.append("tracing")
        return _remote_call_runtime_package_with_extras(extras)

    def _poll_until_ready(
        self,
        *,
        sg_name: str,
        poll_interval: float,
        timeout: float,
        require_ready_replicas: bool = True,
    ) -> None:
        deadline = time.monotonic() + timeout
        status = "unknown"
        status_message = ""
        ready_replicas = 0
        available_replicas = 0
        while time.monotonic() < deadline:
            sg = _get_scaling_group(self._client, sg_name)
            status = sg.status
            status_message = sg.status_message or ""
            ready_replicas = sg.ready_replicas
            available_replicas = sg.available_replicas
            self._web_url = sg.web_url or None
            logger.info(
                "  poll: status=%s ready=%s available=%s message=%s",
                status,
                ready_replicas,
                available_replicas,
                status_message or "",
            )
            if _scaling_group_is_ready(
                sg, require_ready_replicas=require_ready_replicas
            ):
                return
            if status in ("Failed", "Error"):
                message = f"Scaling group {sg_name} entered {status} state"
                if status_message:
                    message = f"{message}: {status_message}"
                raise FunctionError(message)
            time.sleep(poll_interval)
        message = (
            f"Scaling group {sg_name} not ready after {timeout}s "
            f"(status={status}, ready={ready_replicas}, "
            f"available={available_replicas})"
        )
        if status_message:
            message = f"{message}: {status_message}"
        raise FunctionError(message)

    def _requires_ready_replicas(self, sg: Any) -> bool:
        """Whether readiness demands a live replica for this scaling group.

        Prefers the server's own spec over the local ``min_replicas``: a handle
        from ``from_name``/``from_id`` never learns the deployed value, so
        trusting the local default would demand a replica from a scale-to-zero
        function that will never have one.

        ``spec`` is a ``ScalingGroupSpec`` wrapper and both it and its nested
        ``scaling_spec`` are optional, so each level is checked before use.
        """
        if isinstance(sg, sg_pb.ScalingGroupResponse) and sg.has_field("spec"):
            group_spec = sg.spec
            scaling_spec = None if group_spec is None else group_spec.scaling_spec
            if scaling_spec is not None:
                return scaling_spec.min_replicas > 0
        return self._min_replicas > 0

    def wait_ready(
        self,
        *,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
    ) -> RemoteFunction:
        """Wait until the backing scaling group is ready to serve.

        A ``min_replicas=0`` function counts as ready once it reaches
        ``ScaledToZero``, matching what :meth:`deploy` waits for.

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
        deadline = time.monotonic() + timeout
        self._wait_for_background_deploy(timeout=timeout)
        if self._version_info is None:
            raise FunctionError("No function version — call .deploy() first.")

        sg_name = self._version_info.scaling_group_name
        # Poll at least once even if the deployment consumed the whole budget:
        # a zero-poll timeout would report "not ready" without ever asking, and
        # the deploy only waits for readiness conditionally, so the function may
        # well be serving by now.
        while True:
            sg = _get_scaling_group(self._client, sg_name)
            status = sg.status
            self._web_url = sg.web_url or None
            if status in ("Failed", "Error"):
                raise FunctionError(f"Scaling group {sg_name} entered {status} state")
            if _scaling_group_is_ready(
                sg, require_ready_replicas=self._requires_ready_replicas(sg)
            ):
                return self
            if time.monotonic() >= deadline:
                break
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
            self._remote_call_auth_token = None
            if client is not None:
                self._close_remote_call_client(client)

            stub = self._call_stub
            self._call_stub = None
            if stub is not None:
                self._close_remote_call_client(stub)

        if queue:
            client = self._queue_call_client
            self._queue_call_client = None
            self._queue_call_auth_token = None
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
        """Lazily create the ConnectRPC client for calling the remote function.

        Raises
        ------
        FunctionError
            If the function has not been deployed.
        """
        if self._call_stub is not None:
            return

        from chalkcompute._gen.chalk.runtime.v1 import (
            remote_python_call_connect as call_connect,
        )

        if self._web_url is None:
            raise FunctionError("Function is not deployed. Call .deploy() first.")

        target, use_tls = grpc_target_from_url(
            self._web_url,
            insecure_port=self._port,
        )

        self._call_stub = call_connect.RemoteCallServiceClientSync(
            rpc_address_from_target(target, use_tls=use_tls),
            protocol=ProtocolType.GRPC,
            interceptors=rpc_interceptors(),
            http_client=SYNC_HTTP_CLIENT,
            read_max_bytes=RPC_READ_MAX_BYTES,
        )

    def _remote_call_metadata(self, *, queue: bool) -> list[tuple[str, str]]:
        uses_web_identity = (
            getattr(self._client, "_uses_web_identity_token", lambda: False)() is True
        )
        service_auth = None if uses_web_identity else service_auth_context_from_env()
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
        token_attr = "_queue_call_auth_token" if queue else "_remote_call_auth_token"
        uses_web_identity = (
            getattr(self._client, "_uses_web_identity_token", lambda: False)() is True
        )
        current_token: str | None = None
        if uses_web_identity:
            self._client._ensure_auth()
            current_token = self._client._access_token

        client = getattr(self, attr)
        token_matches = (
            not uses_web_identity or getattr(self, token_attr, None) == current_token
        )
        if (
            client is not None
            and not self._remote_call_client_is_closed(client)
            and token_matches
        ):
            return client
        if client is not None:
            self._close_remote_call_client(client)
            setattr(self, attr, None)

        if self._web_url is None:
            raise FunctionError("Function is not deployed. Call .deploy() first.")

        from chalkcompute._remote_call_client import RemoteCallClient

        service_auth = None if uses_web_identity else service_auth_context_from_env()
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
        setattr(self, token_attr, current_token)
        return client

    def _call_direct_chunks_python_grpc(
        self,
        feather_bytes: bytes,
        function_name: str,
        metadata: list[tuple[str, str]],
    ) -> list[bytes]:
        from chalkcompute._gen.chalk.runtime.v1 import (
            remote_python_call_pb as call_pb,
        )

        self._ensure_call_stub()

        def requests() -> Any:
            yield call_pb.CallFunctionRequest(
                name=function_name,
                feather_stream=feather_bytes,
            )

        from chalkcompute._remote_call_client import (
            _is_retryable_unavailable_in_place,
            _retry_direct_call,
        )

        responses = _retry_direct_call(
            function_name,
            lambda: list(
                self._call_stub.call_function(requests(), headers=dict(metadata))
            ),
            lambda error: (
                isinstance(error, ConnectRpcError)
                and error.code == Code.UNAVAILABLE
                and _is_retryable_unavailable_in_place(error)
            ),
        )
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
        from chalkcompute._remote_call_client import (
            RemoteCallTransportError,
            _retry_direct_call,
        )

        function_name = self._queue_function_name()
        metadata = self._per_call_metadata()
        return _retry_direct_call(
            function_name,
            lambda: self._with_remote_call_transport(
                queue=False,
                operation="direct call",
                retry_stale=_is_retryable_stale_remote_call_error,
                call=lambda: self._call_direct_chunks_once(
                    feather_bytes,
                    function_name,
                    metadata,
                ),
            ),
            lambda error: isinstance(error, RemoteCallTransportError),
        )

    def _queue_function_name(self) -> str:
        if self._version_info is not None and self._version_info.function_name:
            return self._version_info.function_name
        return self._func_name

    def _serialize_call_args(self, *args: Any, **kwargs: Any) -> bytes:
        import pyarrow as pa
        import pyarrow.ipc as ipc

        values = self._bind_call_arguments(args, kwargs)

        arrays = []
        if self._call_parameter_names:
            for value, serializer in zip(values, self._arrow_input_serializers):
                if serializer is not None:
                    serialized = serializer(value)
                    arrays.append(pa.array([serialized], type=serialized.type))
                else:
                    arrays.append(pa.array([value]))
            names = self._call_parameter_names
        else:
            arrays.append(pa.array([True]))
            names = (_NO_ARGS_SENTINEL_COLUMN,)
        batch = pa.record_batch(arrays, names=names)

        sink = pa.BufferOutputStream()
        with ipc.new_stream(sink, batch.schema) as writer:
            writer.write_batch(batch)
        return sink.getvalue().to_pybytes()

    def _decode_result_chunks(self, chunks: list[bytes]) -> list[Any]:
        import pyarrow.ipc as ipc

        deserializer = self._arrow_result_deserializer

        results: list[Any] = []
        for chunk in chunks:
            reader = ipc.open_stream(chunk)
            for result_batch in reader:
                field_index = result_batch.schema.get_field_index("result")
                if field_index >= 0:
                    result_column = result_batch.column(field_index)
                    if deserializer is None:
                        results.extend(result_column.to_pylist())
                    else:
                        results.extend(
                            None if not value.is_valid else deserializer(value)
                            for value in result_column
                        )
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
        if self._version_info is None:
            raise FunctionError("No function version — call .deploy() first.")

        trace_metadata = self._per_call_metadata()
        stub = self._client.stub(
            catalog_connect.ExternalFunctionCatalogServiceClientSync
        )
        response = self._client.call(
            stub.call_external_function,
            catalog_pb.CallExternalFunctionRequest(
                function=catalog_pb.ExternalFunctionVersionKey(
                    function_name=self._version_info.function_name,
                    version=self._version_info.version or None,
                ),
                body=Oneof(
                    "remote_call_request",
                    remote_call_pb.CallFunctionRequest(
                        name=self._queue_function_name(),
                        feather_stream=feather_bytes,
                    ),
                ),
            ),
            timeout=300,
            extra_headers=dict(trace_metadata) if trace_metadata else None,
        )
        if response.body is None or response.body.field != "remote_call_response":
            raise FunctionError(
                "No remoteCallResponse in CallExternalFunction response"
            )
        if not response.body.value.feather_stream:
            raise FunctionError("No featherStream in CallExternalFunction response")
        return response.body.value.feather_stream

    def with_knowledge_cutoff(
        self, knowledge_cutoff: KnowledgeCutoff
    ) -> RemoteFunction:
        """Return a call-scoped function handle with an overridden request time.

        The returned handle shares this function's deployment and clients but sends
        ``x-chalk-request-timestamp`` on subsequent calls and deferred enqueues.
        """
        # The clone copies deployment fields by value. Wait first so it cannot
        # retain the pre-deploy ``None`` values while sharing a completed Future.
        self._wait_for_background_deploy()
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
        self._wait_for_background_deploy()
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
        self._wait_for_background_deploy()
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
        self._wait_for_background_deploy()
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
        self._wait_for_background_deploy()
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
        self._wait_for_background_deploy()
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

    def versions(
        self,
        *,
        limit: int | None = None,
        include_deleted: bool = False,
    ) -> list[FunctionVersionInfo]:
        """List immutable versions belonging to this external function."""
        self._wait_for_background_deploy()
        if self._function_id is None:
            raise FunctionError("No function ID — call .deploy() first.")
        if limit is not None and limit < 1:
            raise ValueError(f"limit must be positive, got {limit}")

        stub = self._client.stub(
            catalog_connect.ExternalFunctionCatalogServiceClientSync
        )
        versions: list[FunctionVersionInfo] = []
        cursor: str | None = None
        current_id = None if self._version_info is None else self._version_info.id
        while limit is None or len(versions) < limit:
            page_limit = 100 if limit is None else min(100, limit - len(versions))
            response = self._client.call(
                stub.list_external_function_versions,
                catalog_pb.ListExternalFunctionVersionsRequest(
                    function_id=self._function_id,
                    cursor=cursor,
                    limit=page_limit,
                    include_deleted=include_deleted,
                ),
            )
            for entry in response.entries:
                efv = entry.external_function_version
                if efv is not None:
                    versions.append(
                        _parse_function_version(
                            efv,
                            is_current=efv.id == current_id,
                        )
                    )
            cursor = response.next_cursor or None
            if cursor is None:
                break
        return versions[:limit] if limit is not None else versions

    @_instrumented("chalkcompute.function.rollback")
    def rollback(
        self,
        version_id: str,
        *,
        wait_ready: bool = True,
        timeout: float = 300.0,
        poll_interval: float = 0.5,
    ) -> RemoteFunction:
        """Select an existing immutable version without creating a new one."""
        self._wait_for_background_deploy()
        if self._function_id is None:
            raise FunctionError("No function ID — call .deploy() first.")
        if not version_id:
            raise ValueError("version_id must not be empty")

        stub = self._client.stub(
            catalog_connect.ExternalFunctionCatalogServiceClientSync
        )
        response = self._client.call(
            stub.update_external_function,
            catalog_pb.UpdateExternalFunctionRequest(
                external_function_id=self._function_id,
                traffic=catalog_pb.ExternalFunctionTraffic(
                    targets=[
                        catalog_pb.ExternalFunctionTrafficTarget(
                            version_target=Oneof(
                                "external_function_version_id", version_id
                            ),
                            percent=100,
                        )
                    ]
                ),
                update_mask=FieldMask(paths=["traffic"]),
            ),
            timeout=60,
        )
        _apply_external_function(self, response.external_function)
        self._drop_remote_call_clients()
        if wait_ready:
            self.wait_ready(timeout=timeout, poll_interval=poll_interval)
        return self

    @_instrumented("chalkcompute.function.delete")
    def delete(self) -> None:
        """Delete this external function and all of its versions."""
        self._wait_for_background_deploy()
        if self._function_id is not None:
            stub = self._client.stub(
                catalog_connect.ExternalFunctionCatalogServiceClientSync
            )
            self._client.call(
                stub.delete_external_function,
                catalog_pb.DeleteExternalFunctionRequest(
                    function_id=self._function_id,
                ),
            )
            self._function_id = None
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
        self._wait_for_background_deploy()
        if self._function_id is None:
            raise FunctionError("No function ID — call .deploy() first.")
        old_web_url = self._web_url
        old_port = self._port
        response = _get_external_function(
            self._client,
            function_id=self._function_id,
            include_scaling_group=True,
        )
        _apply_external_function(
            self,
            response.external_function,
            scaling_group=response.scaling_group,
        )
        if self._web_url != old_web_url or self._port != old_port:
            self._drop_remote_call_clients(direct=True, queue=False)
        assert self._version_info is not None
        return self._version_info

    @property
    def version_info(self) -> FunctionVersionInfo | None:
        self._wait_for_background_deploy()
        return self._version_info

    @property
    def function_id(self) -> str | None:
        self._wait_for_background_deploy()
        return self._function_id

    @property
    def web_url(self) -> str | None:
        self._wait_for_background_deploy()
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

    def versions(
        self,
        *,
        limit: int | None = None,
        include_deleted: bool = False,
    ) -> list[FunctionVersionInfo]:
        return self._bound().versions(
            limit=limit,
            include_deleted=include_deleted,
        )

    def rollback(self, version_id: str, **kwargs: Any) -> RemoteFunction:
        return self._bound().rollback(version_id, **kwargs)

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
    volumes: list[tuple[str, str] | Volume | VolumeMount] | None = None,
    tags: dict[str, str] | None = None,
    chalk_identity: bool = False,
    authenticated: bool = False,
    readiness_probe: _ReadinessProbe | None = None,
    min_replicas: int = 1,
    max_replicas: int = 1,
    shutdown_delay_seconds: int | None = None,
    scaling_interval_seconds: int | None = None,
    target_cpu_utilization_percentage: int | None = None,
    target_gpu_utilization_percentage: int | None = None,
    target_queue_depth: int | None = None,
    cron_scaling_trigger: _CronScalingTrigger | None = None,
    scale_from_zero_request_policy: _ScaleFromZeroRequestPolicy | None = None,
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
    volumes: list[tuple[str, str] | Volume | VolumeMount] | None = None,
    tags: dict[str, str] | None = None,
    chalk_identity: bool = False,
    authenticated: bool = False,
    readiness_probe: _ReadinessProbe | None = None,
    min_replicas: int = 1,
    max_replicas: int = 1,
    shutdown_delay_seconds: int | None = None,
    scaling_interval_seconds: int | None = None,
    target_cpu_utilization_percentage: int | None = None,
    target_gpu_utilization_percentage: int | None = None,
    target_queue_depth: int | None = None,
    cron_scaling_trigger: _CronScalingTrigger | None = None,
    scale_from_zero_request_policy: _ScaleFromZeroRequestPolicy | None = None,
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
    volumes: list[tuple[str, str] | Volume | VolumeMount] | None = None,
    tags: dict[str, str] | None = None,
    chalk_identity: bool = False,
    authenticated: bool = False,
    readiness_probe: _ReadinessProbe | None = None,
    min_replicas: int = 1,
    max_replicas: int = 1,
    shutdown_delay_seconds: int | None = None,
    scaling_interval_seconds: int | None = None,
    target_cpu_utilization_percentage: int | None = None,
    target_gpu_utilization_percentage: int | None = None,
    target_queue_depth: int | None = None,
    cron_scaling_trigger: _CronScalingTrigger | None = None,
    scale_from_zero_request_policy: _ScaleFromZeroRequestPolicy | None = None,
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

    **Locally**, decoration validates the function, starts deployment in a
    background worker, and returns. The worker:

    1. Derives Arrow schemas from the function's type hints
    2. Builds a deploy image with a handler shim
    3. Registers the function version via ``ExternalFunctionCatalogService``
    4. If ``CHALK_FUNCTION_LOCAL_SDK_COPY=1``, includes local SDK source in
       the deploy image for unreleased feature iteration.

    Deployments of consecutive, distinctly named functions overlap. Remote
    calls and lifecycle operations wait for their function's deployment before
    proceeding. A script containing only decorated definitions also waits for
    all queued deployments before interpreter shutdown.

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
    target_gpu_utilization_percentage
        Target GPU utilization percentage (1-100) that drives autoscaling, read
        from DCGM. Requires ``gpu`` and ``min_replicas >= 1``: the utilization
        series exists only while replicas do, so this trigger cannot scale a
        group up from zero.
    target_queue_depth
        Target pending-request queue depth per replica. When set, the
        autoscaler scales on the function's own queue depth.
    cron_scaling_trigger
        Sets a replica floor during specified cron-defined time windows,
        independent of the other autoscaling triggers.
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
            tags=tags,
            chalk_identity=chalk_identity,
            authenticated=authenticated,
            readiness_probe=readiness_probe,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            shutdown_delay_seconds=shutdown_delay_seconds,
            scaling_interval_seconds=scaling_interval_seconds,
            target_cpu_utilization_percentage=target_cpu_utilization_percentage,
            target_gpu_utilization_percentage=target_gpu_utilization_percentage,
            target_queue_depth=target_queue_depth,
            cron_scaling_trigger=cron_scaling_trigger,
            scale_from_zero_request_policy=scale_from_zero_request_policy,
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
        f._start_background_deploy()
        return f

    if func is not None:
        if not callable(func):
            raise TypeError("function() positional argument must be callable")
        return decorator(func)

    return decorator
