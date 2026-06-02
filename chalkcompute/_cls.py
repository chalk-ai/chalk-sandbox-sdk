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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeVar, overload

from chalkcompute._concurrency import ConcurrencyPolicy
from chalkcompute._connect import ConnectClient
from chalkcompute._function import (
    _CATALOG_SVC,
    _REMOTE_ENV_VAR,
    _SG_SVC,
    FunctionVersionInfo,
    RemoteFunction,
    SerializationFormat,
    _assert_stripped_source_self_contained,
    _build_and_upload,
    _callable_input_names,
    _cleanup_temp_files,
    _entrypoint_source,
    _extract_field_names,
    _is_chalkcompute_decorator,
    _local_install_entrypoint,
    _make_base_image,
    _make_container_spec,
    _normalize_schedule,
    _should_strip_source,
    _source_text_for_deploy,
    _write_temp,
)
from chalkcompute._naming import sanitize_dns_label, validate_dns_label, validate_env
from chalkcompute._quota import QueuePolicy
from chalkcompute._rate_limit import RateLimitPolicy
from chalkcompute._retry import RetryPolicy
from chalkcompute._tracing import _instrumented
from chalkcompute._volume import (
    VolumeDriver,
    VolumeMount,
    _resolve_driver,
    cleanup_owned_volumes,
    resolve_referenced_volume_type,
)

if TYPE_CHECKING:
    from chalkcompute._image import Image as _Image
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

    hints = typing.get_type_hints(method_func)
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
    parts: list[str] = ["import inspect"]

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
                        for event in events:
                            columns = {{k: v.to_pylist() for k, v in event.items()}}
                            n = len(next(iter(columns.values())))
                            per_caller = []
                            for i in range(n):
                                kwargs = {{k: v[i] for k, v in columns.items()}}
                                per_caller.append(await {method_caller}.{method_name}(**kwargs))
                            results.append(per_caller)
                        return results
                    return asyncio.run(_run_all())
                results = []
                for event in events:
                    columns = {{k: v.to_pylist() for k, v in event.items()}}
                    n = len(next(iter(columns.values())))
                    per_caller = []
                    for i in range(n):
                        kwargs = {{k: v[i] for k, v in columns.items()}}
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
                n = len(next(iter(event.values())))
                if _is_sync_gen:
                    def _gen():
                        for i in range(n):
                            kwargs = {{k: v[i].as_py() for k, v in event.items()}}
                            yield from {method_caller}.{method_name}(**kwargs)
                    return _gen()
                elif _is_async_gen:
                    async def _agen():
                        for i in range(n):
                            kwargs = {{k: v[i].as_py() for k, v in event.items()}}
                            async for item in {method_caller}.{method_name}(**kwargs):
                                yield item
                    return _agen()
                elif _is_coroutine:
                    import asyncio
                    results = []
                    for i in range(n):
                        kwargs = {{k: v[i].as_py() for k, v in event.items()}}
                        results.append(asyncio.run({method_caller}.{method_name}(**kwargs)))
                    return results
                else:
                    results = []
                    for i in range(n):
                        kwargs = {{k: v[i].as_py() for k, v in event.items()}}
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
        chalk_client: Any | None = None,
    ) -> None:
        self._user_cls = user_cls
        self._class_name = user_cls.__name__
        if name is not None:
            validate_dns_label(name)
        self._explicit_name = name
        self._image = image
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        validate_env(env)
        self._env = env or {}
        self._secrets: list[_Secret] = list(secrets or [])
        # User-referenced volumes carry no type of their own; their backend
        # (hence mount type) is resolved once at deploy time via
        # resolve_referenced_volume_type, then travels on each per-method
        # deploy request. Never defaulted here.
        self._referenced_volumes: list[tuple[str, str]] = [
            (vol_name, mount_path) for vol_name, mount_path in (volumes or [])
        ]
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
        self._target_cpu_utilization_percentage = target_cpu_utilization_percentage
        self._target_queue_depth = target_queue_depth
        self._serialization_format: SerializationFormat = serialization_format
        self._options = options or {}
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
        self._quota = (
            QueuePolicy.from_int_or_policy(quota) if quota is not None else None
        )

        self._client = ConnectClient(chalk_client=chalk_client)
        self._deployed = False
        # Lazily-built local instance for in-process __call__ on methods.
        self._local_instance_cache: Any = None

        # Scan the class for markers
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

        # List all function versions, paginating, and collect those matching
        entries: list[tuple[dict, dict | None]] = []
        cursor = ""
        while True:
            req: dict[str, Any] = {"includeScalingGroup": True, "limit": 100}
            if cursor:
                req["cursor"] = cursor
            resp = rc._client.rpc(
                _CATALOG_SVC,
                "ListExternalFunctionVersions",
                req,
            )
            if resp.status_code != 200:
                raise ClassError(f"ListExternalFunctionVersions failed: {resp.text}")
            data = resp.json()
            for entry in data.get("entries", []):
                efv = entry["externalFunctionVersion"]
                if efv["functionName"].startswith(f"{name}."):
                    entries.append((efv, entry.get("scalingGroup")))
            cursor = data.get("nextCursor", "")
            if not cursor:
                break

        if not entries:
            raise ClassError(f"No function versions found for class '{name}'")

        for efv, sg in entries:
            method_name = efv["functionName"].split(".", 1)[1]

            rf = RemoteFunction._make_empty()
            rf._func_name = efv["functionName"]
            rf._explicit_name = sanitize_dns_label(efv["functionName"])
            rf._version_info = FunctionVersionInfo(
                id=efv["id"],
                function_name=efv["functionName"],
                version=efv.get("version", 0),
                scaling_group_name=efv.get("scalingGroupName", ""),
            )
            rf._input_field_names = _extract_field_names(efv.get("inputArrowSchema"))
            if sg:
                rf._web_url = sg.get("webUrl")
                rf._port = sg.get("port", 6666)
            else:
                rf._port = 6666

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
        rc._user_cls = None  # type: ignore[assignment]
        rc._class_name = name
        rc._explicit_name = name
        rc._image = None
        rc._cpu = None
        rc._memory = None
        rc._gpu = None
        rc._env = {}
        rc._secrets = []
        rc._referenced_volumes = []
        rc._volumes = []
        rc._volume_driver = None
        rc._min_replicas = 1
        rc._max_replicas = 1
        rc._shutdown_delay_seconds = None
        rc._scaling_interval_seconds = None
        rc._target_cpu_utilization_percentage = None
        rc._target_queue_depth = None
        rc._serialization_format = "pyarrow"
        rc._options = {}
        rc._max_buffer_duration = None
        rc._max_batching_size = None
        rc._schedule = None
        rc._retries = None
        rc._rate_limit = None
        rc._concurrency = None
        rc._quota = None
        rc._client = ConnectClient(chalk_client=chalk_client)
        rc._deployed = False
        rc._local_instance_cache = None
        rc._methods = {}
        rc._before_name = None
        rc._after_name = None
        return rc

    # -- Image building --------------------------------------------------------

    def _source_file_for_deploy(self) -> str:
        try:
            return inspect.getfile(self._user_cls)
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
        source_file = self._source_file_for_deploy()

        temp_files: list[str] = []
        source_path = Path(source_file)

        if _should_strip_source():
            source = _source_text_for_deploy(source_file)
            stripped = _strip_chalkcompute_from_class_source(source)
            entrypoint_sources: list[str] = []
            for method_info in self._methods.values():
                entrypoint_sources.extend(_entrypoint_source(method_info.func))
            _assert_stripped_source_self_contained(
                source,
                stripped,
                label=f"Class {self._class_name!r}",
                entrypoint_sources=entrypoint_sources,
            )
            source_file = _write_temp(stripped)
            temp_files.append(source_file)
        elif not source_path.is_file():
            source_file = _write_temp(_source_text_for_deploy(source_file))
            temp_files.append(source_file)

        deploy_image = _make_base_image(self._image).add_local_file(
            source_file, "/app/user_module.py"
        )

        # Precompute has-self flags for lifecycle hooks
        before_func = (
            self._user_cls.__dict__.get(self._before_name)
            if self._before_name
            else None
        )
        after_func = (
            self._user_cls.__dict__.get(self._after_name) if self._after_name else None
        )

        # Generate one handler shim per method
        for method_info in self._methods.values():
            shim_content = _generate_class_handler_shim(
                class_name=self._user_cls.__name__,
                method_name=method_info.name,
                method_has_self=_has_self(method_info.func),
                before_name=self._before_name,
                before_has_self=_has_self(before_func) if before_func else False,
                after_name=self._after_name,
                after_has_self=_has_self(after_func) if after_func else False,
                batched=self._max_batching_size is not None,
            )
            shim_path = _write_temp(shim_content)
            temp_files.append(shim_path)
            deploy_image = deploy_image.add_local_file(
                shim_path, f"/app/handler_{method_info.name}.py"
            )

        deploy_image = deploy_image.env({"PYTHONPATH": "/app", _REMOTE_ENV_VAR: "1"})

        return deploy_image, temp_files

    # -- Lifecycle -------------------------------------------------------------

    @_instrumented("chalkcompute.cls.deploy")
    def deploy(
        self,
        *,
        poll_interval: float = 0.5,
        build_timeout: float = 600.0,
        ready_timeout: float = 300.0,
    ) -> RemoteClass:
        """Build the image and deploy all methods as function versions.

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
        from google.protobuf.json_format import MessageToDict

        from chalkcompute._progress import (
            progress_add,
            progress_done,
            progress_fail,
            progress_finish,
            progress_update,
        )

        ckey = f"class:{self._class_name}"
        progress_add(ckey, "class", f'Class "{self._class_name}"')

        try:
            # 1. Build deploy image
            deploy_image, temp_files = self._build_deploy_image()

            # 2. Build image + upload lazy files
            image_uri, new_vols, driver = _build_and_upload(
                self._client,
                deploy_image,
                label=f"class '{self._class_name}'",
                volume_prefix="cls-files",
                poll_interval=poll_interval,
                build_timeout=build_timeout,
                progress_parent=ckey,
            )
            # Resolve each user-referenced volume's real mount type from its
            # backend instead of defaulting; a missing volume fails the deploy.
            # Resolved once here, then reused across every per-method spec.
            referenced_volumes = [
                VolumeMount(
                    name=name,
                    mount_path=mount_path,
                    type=resolve_referenced_volume_type(self._client, name),
                )
                for name, mount_path in self._referenced_volumes
            ]
            self._volumes = [*referenced_volumes, *new_vols]
            self._volume_driver = driver
            _cleanup_temp_files(temp_files)

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

                container_spec = _make_container_spec(
                    name=sg_name,
                    image_uri=image_uri,
                    entrypoint=_local_install_entrypoint(handler_cmd),
                    env=self._env,
                    cpu=self._cpu,
                    memory=self._memory,
                    gpu=self._gpu,
                    volumes=self._volumes,
                )

                # Resolve lazy secrets and add to container spec
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
                        "functionName": func_name,
                        "targetQueueDepth": self._target_queue_depth,
                    }

                spec: dict[str, Any] = {
                    "containerSpec": container_spec,
                    "scalingSpec": scaling_spec,
                }

                # Build function-level config
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

                logger.info("Registering method '%s'...", func_name)
                create_request: dict[str, Any] = {
                    "functionName": func_name,
                    "inputArrowSchema": MessageToDict(input_schema),
                    "outputArrowSchema": MessageToDict(output_schema),
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
                    progress_fail(mkey)
                    raise ClassBuildError(
                        f"CreateExternalFunctionVersion failed for "
                        f"'{func_name}': {resp.text}"
                    )

                data = resp.json()
                efv = data["externalFunctionVersion"]
                version_info = FunctionVersionInfo(
                    id=efv["id"],
                    function_name=efv["functionName"],
                    version=efv.get("version", 0),
                    scaling_group_name=efv.get("scalingGroupName", sg_name),
                )

                sg_data = data.get("scalingGroup")
                web_url = sg_data.get("webUrl") if sg_data else None
                port = sg_data.get("port", 6666) if sg_data else 6666
                sg_status = sg_data.get("status", "") if sg_data else ""

                # Build a RemoteFunction proxy for calling this method
                rf = RemoteFunction._make_empty()
                rf._func_name = func_name
                rf._explicit_name = sg_name
                rf._version_info = version_info
                rf._web_url = web_url
                rf._port = port
                rf._input_field_names = _extract_field_names(
                    efv.get("inputArrowSchema")
                )
                method_info.remote_function = rf

                logger.info(
                    "  %s v%s (id=%s)", func_name, version_info.version, version_info.id
                )
                progress_done(mkey)

                if sg_status != "Running":
                    sg_names.append(version_info.scaling_group_name)

            # 5. Poll scaling groups until running
            if sg_names:
                progress_update(ckey, detail="waiting for ready")
            for sg_name in sg_names:
                self._poll_until_running(
                    sg_name=sg_name,
                    poll_interval=poll_interval,
                    timeout=ready_timeout,
                )

            self._deployed = True
            progress_done(ckey, detail="ready")
        except BaseException:
            progress_fail(ckey)
            raise
        finally:
            progress_finish()

        return self

    def _poll_until_running(
        self,
        *,
        sg_name: str,
        poll_interval: float,
        timeout: float,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self._client.rpc(_SG_SVC, "GetScalingGroup", {"name": sg_name})
            if resp.status_code != 200:
                raise ClassError(f"GetScalingGroup failed: {resp.text}")
            sg = resp.json()["scalingGroup"]
            status = sg.get("status", "")

            # Update the web_url on the corresponding RemoteFunction
            web_url = sg.get("webUrl")
            for method_info in self._methods.values():
                rf = method_info.remote_function
                if (
                    rf is not None
                    and rf._version_info is not None
                    and rf._version_info.scaling_group_name == sg_name
                ):
                    rf._web_url = web_url

            logger.info("  poll %s: status=%s", sg_name, status)
            if status in ("Running", "Available"):
                return
            if status in ("Failed", "Error"):
                raise ClassError(f"Scaling group {sg_name} entered {status} state")
            time.sleep(poll_interval)
        raise ClassError(f"Scaling group {sg_name} timed out after {timeout}s")

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
            driver = self._volume_driver or _resolve_driver(self._client)
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
) -> RemoteClass | _NoOpClass: ...


@overload
def cls(
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
) -> Callable[..., RemoteClass | _NoOpClass]: ...


def cls(
    user_cls: type | None = None,
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
    memory
        Memory resource request (e.g. ``"1Gi"``, ``"512Mi"``).
    gpu
        GPU resource request (e.g. ``"nvidia-l4"``, ``"2:nvidia-a100"``).
    name
        Custom class name. Defaults to the class's ``__name__``.
    env
        Environment variables to pass to the class containers.
    secrets
        List of :class:`~chalkcompute.Secret` references to inject.
    volumes
        List of ``(name, mount_path)`` tuples for persistent storage.
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
    target_queue_depth
        Target pending-request queue depth per replica. When set, the
        autoscaler scales on each method's own queue depth.
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
    quota
        Queue quota policy: cap on queued calls per key. Pass an ``int``
        for a simple ``max_queue_depth`` cap with a per-method key, or a
        :class:`~chalkcompute.QueuePolicy` for full control.
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
        )
        rc.deploy()
        return rc

    if user_cls is not None:
        if not isinstance(user_cls, type):
            raise TypeError("cls() positional argument must be a class")
        return decorator(user_cls)

    return decorator
