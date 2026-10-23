"""
Deploy Python classes as versioned remote functions backed by ExternalFunctionCatalogService.

Usage:
    import chalkcompute

    @chalkcompute.cls
    class Adder:
        @chalkcompute.before
        def on_startup(self) -> None:
            print("Adder started")

        @chalkcompute.method
        def add(self, x: int, y: int) -> int:
            return x + y

        @chalkcompute.after
        def on_shutdown(self) -> None:
            print("Adder stopped")

    result = Adder.add.remote(1, 2)   # wire call to the deployed method
    local = Adder.add(1, 2)           # runs the method body in-process
    print(result)
    Adder.delete()

Or imperatively:

    from chalkcompute import RemoteClass

    class Adder:
        @chalkcompute.method
        def add(self, x: int, y: int) -> int:
            return x + y

    rc = RemoteClass(Adder)
    rc.deploy().wait_ready()
    print(rc.add.remote(1, 2))
    rc.delete()

``Adder.add(...)`` always runs the local method body in-process;
``Adder.add.remote(...)`` and ``Adder.add.defer(...)`` are the wire calls.
"""

from __future__ import annotations

import ast
import inspect
import logging
import os
import textwrap
import time
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeVar, overload

from chalkcompute._chalk_identity import ChalkIdentity
from chalkcompute._function_source_location import FunctionSourceLocations
from chalkcompute._logging import _StatusLogger
from chalkcompute._concurrency import ConcurrencyPolicy
from chalkcompute._connect import ConnectClient
from chalkcompute._containerspec import (
    ComputeClass,
    ContainerSpec,
    NetworkPolicy,
    ExecutionBackend,
    validate_replica_and_utilization,
)
from chalkcompute._env import normalize_env
from chalkcompute._deployment_proto import function_config, scaling_spec
from chalkcompute._function_deployment import (
    ensure_external_function,
    resolve_function_volumes,
    function_source_root,
)
from chalkcompute._image_build import normalize_image_volumes
from chalkcompute._secret import prepare_lazy_secrets
from chalkcompute._function import (
    _REMOTE_ENV_VAR,
    _ROW_METADATA_COLUMN,
    FunctionVersionInfo,
    RemoteFunction,
    SerializationFormat,
    _strip_source_for_deploy,
    _callable_input_names,
    _cleanup_temp_files,
    _entrypoint_source,
    _get_external_function,
    _get_scaling_group,
    _is_chalkcompute_decorator,
    _local_install_entrypoint,
    _make_base_image,
    _normalize_schedule,
    _remote_call_container_env,
    _remote_call_runtime_package_with_extras,
    _remote_function_startup_probe,
    _scaling_group_is_ready,
    _should_strip_source,
    _source_text_for_deploy,
    _write_temp,
    _apply_external_function,
)
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import (
    service_connect as catalog_connect,
)
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as catalog_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._naming import sanitize_dns_label
from chalkcompute._quota import QueuePolicy
from chalkcompute._rate_limit import RateLimitPolicy
from chalkcompute._capture import CapturePolicy, capture_options
from chalkcompute._remote_function_tracing import (
    TracingPolicy,
    TracingPolicyConfig,
    normalize_remote_function_tracing,
)
from chalkcompute._retry import RetryPolicy
from chalkcompute._tracing import _instrumented
from chalkcompute._volume import (
    Volume,
    VolumeClient,
    VolumeMount,
    cleanup_owned_volumes,
)

if TYPE_CHECKING:
    from chalkcompute._image import Image as _Image
    from chalkcompute._scaling_group import CronScalingTrigger as _CronScalingTrigger
    from chalkcompute._scaling_group import ReadinessProbe as _ReadinessProbe
    from chalkcompute._scaling_group import (
        ScaleFromZeroRequestPolicy as _ScaleFromZeroRequestPolicy,
    )
    from chalkcompute._secret import Secret as _Secret

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Marker attribute names
# ---------------------------------------------------------------------------

_CHALK_METHOD_MARKER = "_chalk_method"
_CHALK_BEFORE_MARKER = "_chalk_before"
_CHALK_AFTER_MARKER = "_chalk_after"
F = TypeVar("F", bound=Callable[..., Any])

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ClassError(Exception):
    """Base exception for RemoteClass errors."""


class ClassBuildError(ClassError):
    """Raised when the class image build fails."""


# ---------------------------------------------------------------------------
# Marker decorators
# ---------------------------------------------------------------------------


@overload
def method(func: F, /) -> F: ...


@overload
def method() -> Callable[[F], F]: ...


def method(func: F | None = None, /) -> F | Callable[[F], F]:
    """Mark a class method for remote deployment."""

    def decorator(target: F) -> F:
        setattr(target, _CHALK_METHOD_MARKER, True)
        return target

    if func is not None:
        return decorator(func)
    return decorator


@overload
def before(func: F, /) -> F: ...


@overload
def before() -> Callable[[F], F]: ...


def before(func: F | None = None, /) -> F | Callable[[F], F]:
    """Mark a method as the startup hook (called before the server starts)."""

    def decorator(target: F) -> F:
        setattr(target, _CHALK_BEFORE_MARKER, True)
        return target

    if func is not None:
        return decorator(func)
    return decorator


@overload
def after(func: F, /) -> F: ...


@overload
def after() -> Callable[[F], F]: ...


def after(func: F | None = None, /) -> F | Callable[[F], F]:
    """Mark a method as the shutdown hook (called when the process exits)."""

    def decorator(target: F) -> F:
        setattr(target, _CHALK_AFTER_MARKER, True)
        return target

    if func is not None:
        return decorator(func)
    return decorator


# ---------------------------------------------------------------------------
# Internal data
# ---------------------------------------------------------------------------


@dataclass
class _MethodInfo:
    name: str
    func: Callable
    remote_function: RemoteFunction | None = field(default=None, repr=False)


class _BoundRemoteMethod:
    """A deployed ``@method``, as returned by attribute access on a remote class.

    Calling it (``Adder.add(1, 2)``) runs the local method body in-process —
    it does **not** dispatch to the deployed method. ``.remote()`` and
    ``.defer()`` are the wire calls; ``.with_knowledge_cutoff()`` returns a
    call-scoped handle. Unknown attributes (``version_info``, ``web_url``,
    ``poll``, ``purge``, ``stream`` …) proxy to the backing
    :class:`~chalkcompute.RemoteFunction`.

    Parameters
    ----------
    name
        Method name (used in error messages).
    local_callable
        The bound method to run in-process for ``__call__``, or ``None`` when
        there is no local implementation (e.g. a ``from_name`` handle).
    remote_function
        The deployed :class:`RemoteFunction` proxy, when already resolved.
    resolve_remote
        Zero-arg callable that lazily resolves the proxy by name. Used inside a
        container so a method can reach its deployed sibling without an explicit
        ``RemoteFunction.from_name(...)``.
    """

    def __init__(
        self,
        name: str,
        *,
        local_callable: Callable[..., Any] | None = None,
        remote_function: RemoteFunction | None = None,
        resolve_remote: Callable[[], RemoteFunction] | None = None,
    ) -> None:
        self._name = name
        self._local_callable = local_callable
        self._remote_function = remote_function
        self._resolve_remote = resolve_remote

    def _remote(self) -> RemoteFunction:
        if self._remote_function is None:
            if self._resolve_remote is None:
                raise ClassError(
                    f"Method '{self._name}' is not deployed. Call .deploy() first."
                )
            self._remote_function = self._resolve_remote()
        return self._remote_function

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the underlying method locally, in-process."""
        if self._local_callable is None:
            raise ClassError(
                f"Method '{self._name}' has no local implementation to run "
                f"in-process (the class was attached via from_name). Use "
                f".remote() for a synchronous wire call, or .defer() to enqueue one."
            )
        return self._local_callable(*args, **kwargs)

    def remote(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the deployed method over the wire."""
        return self._remote().remote(*args, **kwargs)

    def defer(self, *args: Any, **kwargs: Any) -> Any:
        """Enqueue a wire call and return a handle immediately."""
        return self._remote().defer(*args, **kwargs)

    def with_capture(
        self, *, inputs: bool | None = None, outputs: bool | None = None
    ) -> RemoteFunction:
        return self._remote().with_capture(inputs=inputs, outputs=outputs)

    def with_knowledge_cutoff(self, knowledge_cutoff: Any) -> RemoteFunction:
        """Return a call-scoped remote handle with an overridden request time."""
        return self._remote().with_knowledge_cutoff(knowledge_cutoff)

    def __getattr__(self, attr: str) -> Any:
        # Delegate everything else (version_info, web_url, poll, purge, stream …)
        # to the backing RemoteFunction. Private names never delegate, both to
        # avoid recursion during __init__ and because they are implementation.
        if attr.startswith("_"):
            raise AttributeError(attr)
        return getattr(self._remote(), attr)


# ---------------------------------------------------------------------------
# Source transformation
# ---------------------------------------------------------------------------


def _strip_chalkcompute_from_class_source(source: str) -> str:
    """Remove chalkcompute imports, decorators, and lifecycle calls.

    Like ``function._strip_chalkcompute_from_source`` but uses ``ast.walk()``
    to catch decorators inside ``ClassDef`` bodies (not just top-level nodes).

    Parameters
    ----------
    source
        Python source text to rewrite.

    Returns
    -------
    str
        Rewritten source with chalkcompute references removed.
    """
    from chalkcompute._capture import strip_secret_annotation_markers

    source = strip_secret_annotation_markers(source)
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)

    remove_ranges: list[tuple[int, int]] = []
    decorated_names: set[str] = set()
    chalkcompute_module_names = {"chalkcompute"}
    chalkcompute_decorator_names: set[str] = set()
    chalkcompute_attributes = {"cls", "method", "before", "after"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "chalkcompute":
                    chalkcompute_module_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("chalkcompute"):
                for alias in node.names:
                    if alias.name == "*":
                        chalkcompute_decorator_names.update(chalkcompute_attributes)
                    elif alias.name in chalkcompute_attributes:
                        chalkcompute_decorator_names.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        # Remove: import chalkcompute / from chalkcompute import ...
        if isinstance(node, ast.Import):
            if any(alias.name.startswith("chalkcompute") for alias in node.names):
                remove_ranges.append((node.lineno, node.end_lineno or node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("chalkcompute"):
                remove_ranges.append((node.lineno, node.end_lineno or node.lineno))

        # Remove chalkcompute decorators from functions and classes
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                dec_source = ast.get_source_segment(source, dec)
                if (
                    dec_source is not None and "chalkcompute" in dec_source
                ) or _is_chalkcompute_decorator(
                    dec,
                    module_names=chalkcompute_module_names,
                    decorator_names=chalkcompute_decorator_names,
                    attribute_names=chalkcompute_attributes,
                ):
                    remove_ranges.append((dec.lineno, dec.end_lineno or dec.lineno))
                    decorated_names.add(node.name)

    # Remove method calls on decorated names (e.g. Adder.deploy(), Adder.delete())
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if (
                isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id in decorated_names
            ):
                remove_ranges.append((node.lineno, node.end_lineno or node.lineno))

    removed_lines: set[int] = set()
    for start, end in remove_ranges:
        for ln in range(start, end + 1):
            removed_lines.add(ln)

    kept = [line for i, line in enumerate(lines, start=1) if i not in removed_lines]
    return "".join(kept)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def _build_method_schemas(method_func: Callable[..., Any]) -> tuple[Any, Any]:
    """Derive input/output Arrow schemas from a method's type hints.

    Like ``function._build_schemas`` but skips the ``self`` parameter.

    Parameters
    ----------
    method_func
        Method to inspect.

    Returns
    -------
    tuple
        ``(input_schema, output_schema)`` Arrow schema protos.

    Raises
    ------
    TypeError
        If any parameter or the return type lacks an annotation.
    """
    from chalkcompute._arrow_schema import rich_to_arrow_schema

    hints = typing.get_type_hints(method_func, include_extras=True)
    sig = inspect.signature(method_func)

    input_types: dict[str, type] = {}
    for param_name in sig.parameters:
        if param_name == "self":
            continue
        param_type = hints.get(param_name)
        if param_type is None:
            raise TypeError(f"Parameter '{param_name}' must have a type annotation")
        input_types[param_name] = param_type
    input_schema = rich_to_arrow_schema(input_types)

    return_type = hints.get("return")
    if return_type is None:
        raise TypeError("Method must have a return type annotation")
    output_schema = rich_to_arrow_schema({"result": return_type})

    return input_schema, output_schema


# ---------------------------------------------------------------------------
# Handler shim generation
# ---------------------------------------------------------------------------


def _has_self(func: Callable) -> bool:
    """Return ``True`` if ``func``'s first parameter is ``self``.

    Parameters
    ----------
    func
        Callable to inspect.

    Returns
    -------
    bool
        Whether ``self`` appears in the signature.
    """
    try:
        return "self" in inspect.signature(func).parameters
    except (ValueError, TypeError):
        return False


def _generate_class_handler_shim(
    class_name: str,
    method_name: str,
    method_has_self: bool,
    before_name: str | None,
    before_has_self: bool,
    after_name: str | None,
    after_has_self: bool,
    *,
    batched: bool = False,
    bind_models: bool = False,
) -> str:
    """Generate a handler shim for a single method on a class.

    The shim instantiates the class, wires up lifecycle hooks, and
    delegates Arrow columnar data to the target method.  Methods that
    lack a ``self`` parameter are called as static/plain functions via
    the class rather than the instance.

    Non-batched mode emits ``handler(event, context)``. Batched mode
    emits ``handler(events, contexts)`` that returns a list-of-lists —
    one inner list per coalesced caller. Generators are not supported
    with batching (rejected at deploy time).

    Parameters
    ----------
    class_name
        Name of the user class to import.
    method_name
        Name of the method to invoke.
    method_has_self
        Whether the method has a ``self`` parameter.
    before_name
        Name of the startup hook, if any.
    before_has_self
        Whether the startup hook has a ``self`` parameter.
    after_name
        Name of the shutdown hook, if any.
    after_has_self
        Whether the shutdown hook has a ``self`` parameter.
    batched
        Whether the deployed method enables batching.

    Returns
    -------
    str
        Python source for the handler shim.
    """
    parts: list[str] = []
    if bind_models:
        parts += [
            "from chalkcompute._model import apply_models_from_env as _apply_models",
            "_apply_models()",
            "",
        ]
    parts += ["import contextlib", "import inspect"]

    if after_name is not None:
        parts.append("import atexit")

    parts += [
        f"from user_module import {class_name}",
        "",
        f"_instance = {class_name}()",
        "",
    ]

    if before_name is not None:
        caller = "_instance" if before_has_self else class_name
        parts += [
            "def on_startup():",
            f"    {caller}.{before_name}()",
            "",
        ]

    if after_name is not None:
        caller = "_instance" if after_has_self else class_name
        parts += [
            "def _on_shutdown():",
            f"    {caller}.{after_name}()",
            "",
            "atexit.register(_on_shutdown)",
            "",
        ]

    parts.append(
        textwrap.dedent(f"""\
        _ROW_METADATA_COLUMN = {_ROW_METADATA_COLUMN!r}


        @contextlib.contextmanager
        def _call_scope(context, column, i, as_py):
            # Entered per row rather than once per call because a generator handler is
            # consumed after handler() returns. Imported lazily so a method that reads
            # neither the call context nor row metadata does not need chalkcompute
            # installed in its image.
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

            row_metadata = None
            if column is not None:
                value = column[i]
                row_metadata = value.as_py() if as_py else value
            token = set_call_scope(context, row_metadata)
            try:
                yield
            finally:
                reset_call_scope(token)
        """)
    )

    method_caller = "_instance" if method_has_self else class_name
    if batched:
        parts.append(
            textwrap.dedent(f"""\
            _target = inspect.unwrap({method_caller}.{method_name})
            _is_coroutine = inspect.iscoroutinefunction(_target)


            def handler(events, contexts):
                if _is_coroutine:
                    import asyncio
                    async def _run_all():
                        results = []
                        for ci, event in enumerate(events):
                            context = contexts[ci] if contexts and ci < len(contexts) else None
                            row_metadata = event.get(_ROW_METADATA_COLUMN)
                            row_metadata = (
                                row_metadata.to_pylist() if row_metadata is not None else None
                            )
                            columns = {{
                                k: v.to_pylist()
                                for k, v in event.items()
                                if k != _ROW_METADATA_COLUMN
                            }}
                            n = len(next(iter(columns.values())))
                            per_caller = []
                            for i in range(n):
                                kwargs = {{k: v[i] for k, v in columns.items()}}
                                with _call_scope(context, row_metadata, i, False):
                                    per_caller.append(await {method_caller}.{method_name}(**kwargs))
                            results.append(per_caller)
                        return results
                    return asyncio.run(_run_all())
                results = []
                for ci, event in enumerate(events):
                    context = contexts[ci] if contexts and ci < len(contexts) else None
                    row_metadata = event.get(_ROW_METADATA_COLUMN)
                    row_metadata = (
                        row_metadata.to_pylist() if row_metadata is not None else None
                    )
                    columns = {{
                        k: v.to_pylist()
                        for k, v in event.items()
                        if k != _ROW_METADATA_COLUMN
                    }}
                    n = len(next(iter(columns.values())))
                    per_caller = []
                    for i in range(n):
                        kwargs = {{k: v[i] for k, v in columns.items()}}
                        with _call_scope(context, row_metadata, i, False):
                            per_caller.append({method_caller}.{method_name}(**kwargs))
                    results.append(per_caller)
                return results
            """).rstrip()
        )
    else:
        parts.append(
            textwrap.dedent(f"""\
            _target = inspect.unwrap({method_caller}.{method_name})
            _is_sync_gen = inspect.isgeneratorfunction(_target)
            _is_async_gen = inspect.isasyncgenfunction(_target)
            _is_coroutine = inspect.iscoroutinefunction(_target) and not _is_async_gen


            def handler(event, context):
                row_metadata = event.get(_ROW_METADATA_COLUMN)
                event = {{
                    k: v for k, v in event.items() if k != _ROW_METADATA_COLUMN
                }}
                n = len(next(iter(event.values())))
                if _is_sync_gen:
                    def _gen():
                        for i in range(n):
                            kwargs = {{k: v[i].as_py() for k, v in event.items()}}
                            with _call_scope(context, row_metadata, i, True):
                                yield from {method_caller}.{method_name}(**kwargs)
                    return _gen()
                elif _is_async_gen:
                    async def _agen():
                        for i in range(n):
                            kwargs = {{k: v[i].as_py() for k, v in event.items()}}
                            with _call_scope(context, row_metadata, i, True):
                                async for item in {method_caller}.{method_name}(**kwargs):
                                    yield item
                    return _agen()
                elif _is_coroutine:
                    import asyncio
                    results = []
                    for i in range(n):
                        kwargs = {{k: v[i].as_py() for k, v in event.items()}}
                        with _call_scope(context, row_metadata, i, True):
                            results.append(asyncio.run({method_caller}.{method_name}(**kwargs)))
                    return results
                else:
                    results = []
                    for i in range(n):
                        kwargs = {{k: v[i].as_py() for k, v in event.items()}}
                        with _call_scope(context, row_metadata, i, True):
                            results.append({method_caller}.{method_name}(**kwargs))
                    return results
            """).rstrip()
        )

    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# RemoteClass
# ---------------------------------------------------------------------------


class RemoteClass:
    """A managed class backed by ExternalFunctionCatalogService.

    Deploys each ``@method`` as a separate function version with its own
    scaling group, sharing a single container image.  Lifecycle hooks
    (``@before`` / ``@after``) run in every method's container.

    Most users should prefer the :func:`cls` decorator.

    Examples
    --------
    Deploy a stateful class with an expensive warm-up step, then call
    individual methods remotely:

    >>> import chalkcompute
    >>> from chalkcompute import Image
    >>> img = (
    ...     Image.debian_slim()
    ...     .pip_install(["sentence-transformers"])
    ... )
    >>> @chalkcompute.cls(
    ...     image=img,
    ...     cpu="2",
    ...     memory="4Gi",
    ...     min_replicas=1,
    ...     max_replicas=2,
    ... )
    ... class Embedder:
    ...     @chalkcompute.before
    ...     def load(self):
    ...         from sentence_transformers import SentenceTransformer
    ...         self.model = SentenceTransformer("all-MiniLM-L6-v2")
    ...     @chalkcompute.method
    ...     def embed(self, text: str) -> list[float]:
    ...         return self.model.encode(text).tolist()
    ...     @chalkcompute.method
    ...     def embed_batch(self, texts: list[str]) -> list[list[float]]:
    ...         return self.model.encode(texts).tolist()
    ...     @chalkcompute.after
    ...     def shutdown(self):
    ...         del self.model
    >>> Embedder.deploy()
    >>> e = Embedder()
    >>> vec = e.embed("hello world")
    >>> batch = e.embed_batch(["a", "b", "c"])
    """

    def __init__(
        self,
        user_cls: type,
        *,
        image: _Image | None = None,
        name: str | None = None,
        cpu: str | None = None,
        compute_class: ComputeClass | None = None,
        network_policy: NetworkPolicy | None = None,
        execution_target: ExecutionBackend | None = None,
        memory: str | None = None,
        gpu: str | None = None,
        env: Mapping[str, str | _Secret] | None = None,
        secrets: list[_Secret] | None = None,
        volumes: list[tuple[str | Volume, str] | Volume | VolumeMount] | None = None,
        tags: dict[str, str] | None = None,
        chalk_identity: bool | ChalkIdentity = False,
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
        queue_policy: QueuePolicy | None = None,
        schedule: str | None = None,
        capture: bool | CapturePolicy | None = None,
        tracing: TracingPolicyConfig | None = None,
        models: typing.Mapping[str, Any] | None = None,
        chalk_client: Any | None = None,
    ) -> None:
        self._user_cls: type | None = user_cls
        self._class_name = user_cls.__name__
        env, secrets = normalize_env(env, secrets)
        normalized_image, resolved_volumes = normalize_image_volumes(image, volumes)
        if isinstance(normalized_image, str):
            raise TypeError("class images must be Image instances")
        ContainerSpec.validate_fields(
            image=normalized_image,
            name=name,
            cpu=cpu,
            compute_class=compute_class,
            network_policy=network_policy,
            execution_target=execution_target,
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
        self._image = normalized_image
        self._cpu = cpu
        self._compute_class = compute_class
        self._network_policy = network_policy
        self._execution_target = execution_target
        self._memory = memory
        self._gpu = gpu
        self._env = dict(env) if env else {}
        if models:
            from chalkcompute._model import MODELS_ENV_VAR, encode_models_env

            self._env[MODELS_ENV_VAR] = encode_models_env(models)
        self._secrets: list[_Secret] = list(secrets or [])
        # Existence is checked once at deploy time, not here.
        self._referenced_volumes: list[VolumeMount] = resolved_volumes
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
        self._options = capture_options(options or {}, capture)
        self._max_buffer_duration = max_buffer_duration
        self._max_batching_size = max_batching_size
        self._schedule = _normalize_schedule(
            schedule,
            owner=f"Class '{self._class_name}' schedule",
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
        if queue_policy is not None and not isinstance(queue_policy, QueuePolicy):
            raise TypeError(
                f"queue_policy must be a QueuePolicy, got {type(queue_policy).__name__}"
            )
        self._queue_policy = queue_policy
        self._tracing = normalize_remote_function_tracing(tracing)

        self._client = ConnectClient(chalk_client=chalk_client)
        self._deployed = False
        # Lazily-built local instance for in-process __call__ on methods.
        self._local_instance_cache: Any = None

        # Scan the class for markers
        self._source_locations: dict[str, catalog_pb.FunctionSourceLocation | None] = {}
        self._methods: dict[str, _MethodInfo] = {}
        self._before_name: str | None = None
        self._after_name: str | None = None

        for attr_name in list(user_cls.__dict__):
            attr = user_cls.__dict__[attr_name]
            if not callable(attr):
                continue
            if getattr(attr, _CHALK_METHOD_MARKER, False):
                self._methods[attr_name] = _MethodInfo(name=attr_name, func=attr)
            if getattr(attr, _CHALK_BEFORE_MARKER, False):
                if self._before_name is not None:
                    raise ClassError("Only one @before method is allowed per class")
                self._before_name = attr_name
            if getattr(attr, _CHALK_AFTER_MARKER, False):
                if self._after_name is not None:
                    raise ClassError("Only one @after method is allowed per class")
                self._after_name = attr_name

        if not self._methods:
            raise ClassError(
                f"Class {user_cls.__name__} has no @method decorated methods"
            )

        if self._schedule is not None:
            for mname, mi in self._methods.items():
                input_names = _callable_input_names(mi.func, skip_self=True)
                if input_names:
                    raise ClassError(
                        f"Scheduled method '{self._class_name}.{mname}' cannot have "
                        f"inputs: {', '.join(input_names)}"
                    )
                if inspect.isgeneratorfunction(mi.func) or inspect.isasyncgenfunction(
                    mi.func
                ):
                    raise ClassError(
                        f"Scheduled method '{self._class_name}.{mname}' cannot be a "
                        f"generator; scheduled invocations have no caller to consume "
                        f"yielded values."
                    )

        if max_batching_size is not None:
            for mname, mi in self._methods.items():
                if inspect.isgeneratorfunction(mi.func) or inspect.isasyncgenfunction(
                    mi.func
                ):
                    raise ClassError(
                        f"Method '{self._class_name}.{mname}' is a generator; "
                        f"generators are not supported with max_batching_size. "
                        f"Remove max_batching_size or change the method to return "
                        f"a value."
                    )

    # -- Alternate constructors ------------------------------------------------

    @classmethod
    def from_name(cls, name: str, *, chalk_client: Any | None = None) -> RemoteClass:
        """Attach to an existing remote class by name.

        Discovers all function versions whose ``functionName`` starts with
        ``{name}.`` and rebuilds a ``RemoteClass`` with callable methods.

        Parameters
        ----------
        name
            Class name previously used at deploy time.

        Returns
        -------
        RemoteClass
            A handle bound to the existing versions.

        Raises
        ------
        ClassError
            If no function versions are found for the given name.
        """
        rc = cls._make_empty(name, chalk_client=chalk_client)

        # Find stable function parents for every deployed class method.
        function_ids: list[str] = []
        cursor: str | None = None
        stub = rc._client.stub(catalog_connect.ExternalFunctionCatalogServiceClientSync)
        while True:
            response = rc._client.call(
                stub.list_external_functions,
                catalog_pb.ListExternalFunctionsRequest(
                    limit=100,
                    cursor=cursor,
                    search=f"{name}.",
                ),
            )
            function_ids.extend(
                summary.function_id
                for summary in response.functions
                if summary.name.startswith(f"{name}.")
            )
            cursor = response.next_cursor or None
            if cursor is None:
                break

        if not function_ids:
            raise ClassError(f"No function versions found for class '{name}'")

        for function_id in function_ids:
            function_response = _get_external_function(
                rc._client,
                function_id=function_id,
                include_scaling_group=True,
            )
            external_function = function_response.external_function
            if external_function is None or external_function.current_version is None:
                continue
            efv = external_function.current_version
            method_name = efv.function_name.split(".", 1)[1]

            rf = RemoteFunction._make_empty()
            _apply_external_function(
                rf,
                external_function,
                scaling_group=function_response.scaling_group,
            )

            rc._methods[method_name] = _MethodInfo(
                name=method_name,
                func=lambda *a, **kw: None,
                remote_function=rf,
            )

        rc._deployed = True
        return rc

    @classmethod
    def _make_empty(cls, name: str, *, chalk_client: Any | None = None) -> RemoteClass:
        rc = cls.__new__(cls)
        rc._user_cls = None
        rc._class_name = name
        rc._explicit_name = name
        rc._image = None
        rc._cpu = None
        rc._compute_class = None
        rc._network_policy = None
        rc._execution_target = None
        rc._memory = None
        rc._gpu = None
        rc._env = {}
        rc._secrets = []
        rc._referenced_volumes = []
        rc._volumes = []
        rc._tags = {}
        rc._chalk_identity = False
        rc._authenticated = False
        rc._readiness_probe = None
        rc._scale_from_zero_request_policy = None
        rc._volume_driver = None
        rc._min_replicas = 1
        rc._max_replicas = 1
        rc._shutdown_delay_seconds = None
        rc._scaling_interval_seconds = None
        rc._target_cpu_utilization_percentage = None
        rc._target_gpu_utilization_percentage = None
        rc._target_queue_depth = None
        rc._cron_scaling_trigger = None
        rc._serialization_format = "pyarrow"
        rc._options = {}
        rc._max_buffer_duration = None
        rc._max_batching_size = None
        rc._schedule = None
        rc._retries = None
        rc._rate_limit = None
        rc._concurrency = None
        rc._queue_policy = None
        rc._tracing = None
        rc._client = ConnectClient(chalk_client=chalk_client)
        rc._deployed = False
        rc._local_instance_cache = None
        rc._methods = {}
        rc._before_name = None
        rc._after_name = None
        return rc

    # -- Image building --------------------------------------------------------

    def _require_user_cls(self) -> type:
        if self._user_cls is None:
            raise ClassBuildError(
                f"Class {self._class_name!r} has no local implementation to deploy"
            )
        return self._user_cls

    def _remote_call_runtime_package(self) -> str:
        from chalkcompute._capture import require_capture_runtime

        require_capture_runtime(self._options)
        extras: list[str] = []
        if isinstance(self._tracing, TracingPolicy):
            extras.append("tracing")
        return _remote_call_runtime_package_with_extras(extras)

    def _source_file_for_deploy(self) -> str:
        user_cls = self._require_user_cls()
        try:
            return inspect.getfile(user_cls)
        except (OSError, TypeError) as class_error:
            for method_info in self._methods.values():
                try:
                    return inspect.getfile(method_info.func)
                except (OSError, TypeError):
                    continue
            raise ClassBuildError(
                f"Could not determine source file for class {self._class_name!r}"
            ) from class_error

    def _build_deploy_image(self) -> tuple[Any, list[str]]:
        """Build the deploy image with all handler shims and user module.

        Returns
        -------
        tuple
            ``(Image, list_of_temp_file_paths)``.
        """
        user_cls = self._require_user_cls()
        source_file = self._source_file_for_deploy()
        source_root = function_source_root(self._image, self._referenced_volumes)

        temp_files: list[str] = []
        source_path = Path(source_file)
        source = _source_text_for_deploy(source_file)

        if _should_strip_source():
            entrypoint_sources: list[str] = []
            for method_info in self._methods.values():
                entrypoint_sources.extend(_entrypoint_source(method_info.func))
            source = _strip_source_for_deploy(
                source,
                strip=_strip_chalkcompute_from_class_source,
                label=f"Class {self._class_name!r}",
                entrypoint_sources=entrypoint_sources,
            )
            source_file = _write_temp(source)
            temp_files.append(source_file)
        elif not source_path.is_file():
            source_file = _write_temp(source)
            temp_files.append(source_file)

        deploy_image = _make_base_image(
            self._image,
            remote_call_runtime_package=self._remote_call_runtime_package(),
        ).add_local_file(source_file, f"{source_root}/user_module.py")

        source_locations = FunctionSourceLocations(
            source, f"{source_root}/user_module.py"
        )
        self._source_locations = {
            method.name: source_locations.get(
                getattr(inspect.unwrap(method.func), "__qualname__", None),
            )
            for method in self._methods.values()
        }

        # Precompute has-self flags for lifecycle hooks
        before_func = (
            user_cls.__dict__.get(self._before_name) if self._before_name else None
        )
        after_func = (
            user_cls.__dict__.get(self._after_name) if self._after_name else None
        )

        from chalkcompute._model import MODELS_ENV_VAR

        bind_models = bool(self._env.get(MODELS_ENV_VAR))
        # Generate one handler shim per method
        for method_info in self._methods.values():
            shim_content = _generate_class_handler_shim(
                class_name=user_cls.__name__,
                method_name=method_info.name,
                method_has_self=_has_self(method_info.func),
                before_name=self._before_name,
                before_has_self=_has_self(before_func) if before_func else False,
                after_name=self._after_name,
                after_has_self=_has_self(after_func) if after_func else False,
                batched=self._max_batching_size is not None,
                bind_models=bind_models,
            )
            shim_path = _write_temp(shim_content)
            temp_files.append(shim_path)
            deploy_image = deploy_image.add_local_file(
                shim_path, f"{source_root}/handler_{method_info.name}.py"
            )

        deploy_image = deploy_image.env(
            {
                "PYTHONPATH": source_root
                if source_root == "/app"
                else f"{source_root}:/app",
                _REMOTE_ENV_VAR: "1",
            }
        )

        return deploy_image, temp_files

    # -- Lifecycle -------------------------------------------------------------

    @_instrumented("chalkcompute.cls.deploy")
    def deploy(
        self,
        *,
        poll_interval: float = 0.5,
        build_timeout: float = 600.0,
        ready_timeout: float = 300.0,
        force_new_version: bool = False,
    ) -> RemoteClass:
        """Deploy all methods, reusing unchanged function versions.

        ``force_new_version=True`` creates fresh versions while still reusing
        image builds and source uploads.

        Parameters
        ----------
        poll_interval
            Seconds between status polls.
        build_timeout
            Maximum seconds to wait for the image build.
        ready_timeout
            Maximum seconds to wait for scaling groups to become ready.

        Returns
        -------
        RemoteClass
            ``self`` for chaining.
        """
        from chalkcompute._progress import (
            progress_add,
            progress_done,
            progress_fail,
            progress_finish,
            progress_update,
        )

        ckey = f"class:{self._class_name}"
        progress_add(ckey, "class", f'Class "{self._class_name}"')

        temp_files: list[str] = []
        try:
            # 1. Build deploy image
            deploy_image, temp_files = self._build_deploy_image()
            self._volumes = list(self._referenced_volumes)
            self._volume_driver = None
            deploy_volumes = resolve_function_volumes(
                self._client,
                image=deploy_image,
                volume_name=self._explicit_name or self._class_name,
                referenced=self._volumes,
                poll_interval=poll_interval,
                timeout=build_timeout,
            )
            resolved_secrets, local_secrets = prepare_lazy_secrets(self._secrets)

            # 3. Register each method as a function version
            sg_names: list[str] = []
            for method_info in self._methods.values():
                input_schema, output_schema = _build_method_schemas(method_info.func)

                func_name = (
                    f"{self._explicit_name or self._class_name}.{method_info.name}"
                )
                sg_name = sanitize_dns_label(func_name)

                mkey = f"method:{func_name}"
                progress_add(mkey, "method", f".{method_info.name}()", parent=ckey)

                handler_cmd = [
                    "chalk-remote-call",
                    "--handler",
                    f"handler_{method_info.name}.handler",
                ]

                container_spec = ContainerSpec(
                    name=sg_name,
                    image="",
                    entrypoint=_local_install_entrypoint(handler_cmd),
                    env=_remote_call_container_env(
                        self._env, deploy_image._env.get("PYTHONPATH", "/app")
                    ),
                    cpu=self._cpu,
                    compute_class=self._compute_class,
                    network_policy=self._network_policy,
                    execution_target=self._execution_target,
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
                        function_name=func_name,
                        target_queue_depth=self._target_queue_depth,
                        cron_scaling_trigger=self._cron_scaling_trigger,
                        scale_from_zero_request_policy=(
                            self._scale_from_zero_request_policy
                        ),
                    ),
                )

                # Build function-level config
                config = function_config(
                    serialization_format=self._serialization_format,
                    options=self._options,
                    max_buffer_duration=self._max_buffer_duration,
                    max_batching_size=self._max_batching_size,
                    retries=self._retries,
                    rate_limit=self._rate_limit,
                    concurrency=self._concurrency,
                    queue_policy=self._queue_policy,
                    schedule=self._schedule,
                    tracing=self._tracing,
                )

                config.source_location = self._source_locations.get(method_info.name)

                logger.info("Registering method '%s'...", func_name)
                ensured = ensure_external_function(
                    self._client,
                    function_name=func_name,
                    image=deploy_image,
                    local_secrets=local_secrets,
                    force_new_version=force_new_version,
                    poll_interval=poll_interval,
                    timeout=build_timeout,
                    version_spec=catalog_pb.ExternalFunctionVersionSpec(
                        input_arrow_schema=input_schema,
                        output_arrow_schema=output_schema,
                        spec=spec,
                        config=config,
                    ),
                )
                external_function = ensured.external_function
                if external_function is None:
                    raise ClassBuildError(
                        "Ensure response omitted the external function"
                    )
                efv = external_function.current_version
                if efv is None:
                    progress_fail(mkey)
                    raise ClassBuildError(
                        f"External-function mutation returned no version for {func_name!r}"
                    )

                # Build a RemoteFunction proxy for calling this method
                rf = RemoteFunction._make_empty()
                _apply_external_function(
                    rf, external_function, scaling_group=ensured.scaling_group
                )
                method_info.remote_function = rf
                version_info = rf.version_info
                assert version_info is not None
                sg_data = ensured.scaling_group

                logger.info(
                    "  %s v%s (id=%s)", func_name, version_info.version, version_info.id
                )
                progress_done(mkey)

                if not (
                    sg_data
                    and _scaling_group_is_ready(
                        sg_data,
                        require_ready_replicas=self._min_replicas > 0,
                    )
                ):
                    sg_names.append(version_info.scaling_group_name)

            # 5. Poll scaling groups until their backing pods can serve.
            if sg_names:
                progress_update(ckey, detail="Waiting for readiness")
            for sg_name in sg_names:
                self._poll_until_ready(
                    sg_name=sg_name,
                    poll_interval=poll_interval,
                    timeout=ready_timeout,
                    require_ready_replicas=self._min_replicas > 0,
                )

            self._deployed = True
            progress_done(ckey, detail="ready")
            logger.info("Class ready: %s", self._class_name)
        except BaseException:
            progress_fail(ckey)
            raise
        finally:
            _cleanup_temp_files(temp_files)
            progress_finish()

        return self

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
        status_log = _StatusLogger(
            logger, f"Scaling group {sg_name}", progress_key=f"class:{self._class_name}"
        )
        ready_replicas = 0
        available_replicas = 0
        while time.monotonic() < deadline:
            sg = _get_scaling_group(self._client, sg_name)
            status = sg.status
            ready_replicas = sg.ready_replicas
            available_replicas = sg.available_replicas

            # Update the web_url on the corresponding RemoteFunction
            web_url = sg.web_url or None
            for method_info in self._methods.values():
                rf = method_info.remote_function
                if (
                    rf is not None
                    and rf._version_info is not None
                    and rf._version_info.scaling_group_name == sg_name
                ):
                    rf._web_url = web_url

            status_log.update(
                status,
                ready=ready_replicas,
                available=available_replicas,
                message=sg.status_message or "",
            )
            if _scaling_group_is_ready(
                sg, require_ready_replicas=require_ready_replicas
            ):
                return
            if status in ("Failed", "Error"):
                raise ClassError(f"Scaling group {sg_name} entered {status} state")
            time.sleep(poll_interval)
        raise ClassError(
            f"Scaling group {sg_name} not ready after {timeout}s "
            f"(status={status}, ready={ready_replicas}, "
            f"available={available_replicas})"
        )

    def wait_ready(
        self,
        *,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
    ) -> RemoteClass:
        """Wait until all backing scaling groups have ready replicas.

        Parameters
        ----------
        timeout
            Maximum seconds to wait per method.
        poll_interval
            Seconds between status polls.

        Returns
        -------
        RemoteClass
            ``self`` for chaining.
        """
        for method_info in self._methods.values():
            if method_info.remote_function is not None:
                method_info.remote_function.wait_ready(
                    timeout=timeout,
                    poll_interval=poll_interval,
                )
        return self

    @_instrumented("chalkcompute.cls.delete")
    def delete(self) -> None:
        """Delete all function versions and clean up volumes."""
        for method_info in self._methods.values():
            if method_info.remote_function is not None:
                method_info.remote_function.delete()
                method_info.remote_function = None

        if self._volumes:
            # Driver is used here only to perform the delete action, not to
            # derive a mount type.
            driver = self._volume_driver or VolumeClient.from_connect(self._client)
            cleanup_owned_volumes(driver, self._volumes, self._referenced_volumes)
        self._volumes = []
        self._volume_driver = None
        self._deployed = False

    def refresh(self) -> dict[str, FunctionVersionInfo]:
        """Fetch latest info for all methods from the server.

        Returns
        -------
        dict
            Mapping of method name to updated ``FunctionVersionInfo``.
        """
        result: dict[str, FunctionVersionInfo] = {}
        for method_info in self._methods.values():
            if method_info.remote_function is not None:
                result[method_info.name] = method_info.remote_function.refresh()
        return result

    # -- Attribute access for method calls -------------------------------------

    def __getattr__(self, name: str) -> Any:
        # Avoid infinite recursion for private/dunder attributes
        if name.startswith("_"):
            raise AttributeError(name)
        methods = object.__getattribute__(self, "_methods")
        if name in methods:
            mi = methods[name]
            if mi.remote_function is None:
                raise ClassError(
                    f"Method '{name}' not deployed yet. Call .deploy() first."
                )
            return _BoundRemoteMethod(
                name,
                local_callable=self._local_method(name),
                remote_function=mi.remote_function,
            )
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")

    def _local_method(self, name: str) -> Callable[..., Any] | None:
        """Return the bound method to run in-process, or ``None`` if unavailable.

        ``from_name`` handles have no local class (``_user_cls is None``); their
        methods can only be reached over the wire via ``.remote()`` / ``.defer()``.
        """
        if self._user_cls is None:
            return None
        if self._local_instance_cache is None:
            self._local_instance_cache = self._user_cls()
        return getattr(self._local_instance_cache, name)

    # -- Properties ------------------------------------------------------------

    @property
    def class_name(self) -> str:
        return self._class_name

    @property
    def deployed(self) -> bool:
        return self._deployed

    def __repr__(self) -> str:
        status = "deployed" if self._deployed else "not deployed"
        methods = ", ".join(self._methods.keys())
        return f"RemoteClass({self._class_name!r}, methods=[{methods}], {status})"


# ---------------------------------------------------------------------------
# No-op wrapper (in-container)
# ---------------------------------------------------------------------------


class _NoOpClass:
    """The decorated class as seen *inside* a deployed container (``_REMOTE_ENV_VAR``).

    Lifecycle methods (``deploy``, ``wait_ready`` …) are no-ops. Accessing a
    ``@method`` returns a :class:`_BoundRemoteMethod`: calling it runs the local
    method in-process, while ``.remote()`` / ``.defer()`` dispatch to the
    deployed method (resolved lazily by name) — so a method can invoke a
    sibling without an explicit ``RemoteClass.from_name(...)``. Non-method
    attributes pass through to the local instance unchanged.
    """

    def __init__(self, user_cls: type, *, name: str | None = None) -> None:
        self._instance = user_cls()
        self._registered_name = name or user_cls.__name__
        # Deployed-method proxies, resolved once by name and shared across the
        # fresh _BoundRemoteMethod handed out on each attribute access.
        self._remote_methods: dict[str, RemoteFunction] = {}

    def _resolve_method(self, name: str) -> RemoteFunction:
        if name not in self._remote_methods:
            self._remote_methods[name] = RemoteFunction.from_name(
                f"{self._registered_name}.{name}"
            )
        return self._remote_methods[name]

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self._instance, name)
        marked = getattr(
            type(self._instance).__dict__.get(name), _CHALK_METHOD_MARKER, False
        )
        if marked:
            return _BoundRemoteMethod(
                name,
                local_callable=attr,
                resolve_remote=lambda: self._resolve_method(name),
            )
        return attr

    def __call__(self) -> _NoOpClass:
        """Behave like a zero-arg constructor in remote handler imports.

        Returns
        -------
        _NoOpClass
            ``self``.
        """
        return self

    def deploy(self, **_: Any) -> _NoOpClass:
        return self

    def wait_ready(self, **_: Any) -> _NoOpClass:
        return self

    def delete(self) -> None:
        pass

    def refresh(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


@overload
def cls(
    user_cls: type,
    /,
    *,
    image: _Image | None = None,
    cpu: str | None = None,
    compute_class: ComputeClass | None = None,
    network_policy: NetworkPolicy | None = None,
    execution_target: ExecutionBackend | None = None,
    memory: str | None = None,
    gpu: str | None = None,
    name: str | None = None,
    env: Mapping[str, str | _Secret] | None = None,
    secrets: list[_Secret] | None = None,
    volumes: list[tuple[str | Volume, str] | Volume | VolumeMount] | None = None,
    tags: dict[str, str] | None = None,
    chalk_identity: bool | ChalkIdentity = False,
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
    queue_policy: QueuePolicy | None = None,
    schedule: str | None = None,
    models: typing.Mapping[str, Any] | None = None,
    capture: bool | CapturePolicy | None = None,
    tracing: TracingPolicyConfig | None = None,
) -> RemoteClass | _NoOpClass: ...


@overload
def cls(
    *,
    image: _Image | None = None,
    cpu: str | None = None,
    compute_class: ComputeClass | None = None,
    network_policy: NetworkPolicy | None = None,
    execution_target: ExecutionBackend | None = None,
    memory: str | None = None,
    gpu: str | None = None,
    name: str | None = None,
    env: Mapping[str, str | _Secret] | None = None,
    secrets: list[_Secret] | None = None,
    volumes: list[tuple[str | Volume, str] | Volume | VolumeMount] | None = None,
    tags: dict[str, str] | None = None,
    chalk_identity: bool | ChalkIdentity = False,
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
    queue_policy: QueuePolicy | None = None,
    schedule: str | None = None,
    models: typing.Mapping[str, Any] | None = None,
    capture: bool | CapturePolicy | None = None,
    tracing: TracingPolicyConfig | None = None,
) -> Callable[..., RemoteClass | _NoOpClass]: ...


def cls(
    user_cls: type | None = None,
    /,
    *,
    image: _Image | None = None,
    cpu: str | None = None,
    compute_class: ComputeClass | None = None,
    network_policy: NetworkPolicy | None = None,
    execution_target: ExecutionBackend | None = None,
    memory: str | None = None,
    gpu: str | None = None,
    name: str | None = None,
    env: Mapping[str, str | _Secret] | None = None,
    secrets: list[_Secret] | None = None,
    volumes: list[tuple[str | Volume, str] | Volume | VolumeMount] | None = None,
    tags: dict[str, str] | None = None,
    chalk_identity: bool | ChalkIdentity = False,
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
    queue_policy: QueuePolicy | None = None,
    schedule: str | None = None,
    models: typing.Mapping[str, Any] | None = None,
    capture: bool | CapturePolicy | None = None,
    tracing: TracingPolicyConfig | None = None,
) -> Callable[..., RemoteClass | _NoOpClass] | RemoteClass | _NoOpClass:
    """Decorator that deploys a class as versioned remote functions.

    **Locally** (at decoration time):

    1. Scans the class for ``@method``, ``@before``, ``@after``
    2. Builds a deploy image with per-method handler shims
    3. Registers each method via ``ExternalFunctionCatalogService``
    4. If ``CHALK_FUNCTION_LOCAL_SDK_COPY=1``, includes local SDK source in
       the deploy image for unreleased feature iteration.

    **Remotely** (inside the container):
    Lifecycle methods become no-ops. Accessing a ``@method`` runs the local
    method body in-process when called; ``.remote()`` / ``.defer()`` dispatch to
    the deployed method (resolved lazily by name), so a method can invoke a
    sibling without ``RemoteClass.from_name(...)``.

    Parameters
    ----------
    image
        Container image to deploy in. Defaults to ``Image.debian_slim()``.
    cpu
        CPU resource request (e.g. ``"1"``, ``"500m"``).
    compute_class
        Where to run replicas. Omit to use the server default; HOST requires fixed,
        positive replica counts.
    network_policy
        Optional host egress policy. Omit for the server default;
        ``NetworkPolicy()`` denies all egress. Kubernetes does not support it.
    execution_target
        Optional :class:`ExecutionBackend`; defaults to the environment cluster.
    memory
        Memory resource request (e.g. ``"1Gi"``, ``"512Mi"``).
    gpu
        GPU resource request (e.g. ``"nvidia-l4"``, ``"2:nvidia-a100"``).
    name
        Optional registered-name override. Omit this in normal usage to use
        the class's ``__name__``. Set it only when a different registered name
        is intentional, such as preserving existing deployments after renaming
        the Python class. Changing or removing an override changes the target
        functions; it does not rename existing deployments.
    env
        Environment variables as strings or single-value Secret references.
        The mapping key sets the injected name, overriding aliases and prefixes.
    secrets
        List of :class:`~chalkcompute.Secret` references to inject.
    volumes
        List of ``Volume`` handles, ``VolumeMount`` specifications, or
        ``(name_or_volume, mount_path)`` tuples for persistent storage.
        Volume handles preserve their selected ref or pinned version.
    min_replicas
        Minimum number of replicas per method.
    max_replicas
        Maximum number of replicas per method.
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
        autoscaler scales on each method's own queue depth.
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
        Retry policy for handler invocations. An ``int`` is the number of
        retries after the initial execution. Redis Stream function queues
        currently honor ``max_retries`` with a fixed reclaim delay; the
        backoff, jitter, key, and exception-filter fields are reserved for
        other invocation paths.
    rate_limit
        Rate limit policy for outbound calls. Pass an ``int`` for a simple
        "N per second" cap with a per-method key, or a
        :class:`~chalkcompute.RateLimitPolicy` for full control (``rate``,
        ``per``, ``key``). Multiple methods sharing the same ``key`` share
        one bucket — used when a downstream service imposes a throughput
        limit across callers.
    concurrency
        Concurrency policy: cap on in-flight handler invocations. Pass an
        ``int`` for a simple ``max_concurrent`` cap with a per-method key,
        or a :class:`~chalkcompute.ConcurrencyPolicy` for full control
        (``max_concurrent``, ``key``). Multiple methods sharing the same
        ``key`` share one gate — independent of ``rate_limit`` (which caps
        rate, not in-flight count).
    queue_policy
        Queue configuration shared by the class's methods, including the
        pending-item cap and asynchronous result retention. Pass a
        :class:`~chalkcompute.QueuePolicy`; terminal results are retained for
        one day by default or for ``result_ttl_seconds`` when configured.
    schedule
        Optional crontab or Chalk duration string applied to every no-input
        ``@method`` in the class (e.g. ``"0 * * * *"`` or ``"1h"``).
    """

    def decorator(user_cls: type) -> RemoteClass | _NoOpClass:
        if os.environ.get(_REMOTE_ENV_VAR):
            return _NoOpClass(user_cls, name=name)

        rc = RemoteClass(
            user_cls,
            image=image,
            name=name,
            cpu=cpu,
            compute_class=compute_class,
            network_policy=network_policy,
            execution_target=execution_target,
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
            queue_policy=queue_policy,
            schedule=schedule,
            capture=capture,
            tracing=tracing,
            models=models,
        )
        rc.deploy()
        return rc

    if user_cls is not None:
        if not isinstance(user_cls, type):
            raise TypeError("cls() positional argument must be a class")
        return decorator(user_cls)

    return decorator
