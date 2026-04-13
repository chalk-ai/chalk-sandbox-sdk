"""
Deploy Python classes as versioned remote functions backed by ExternalFunctionCatalogService.

Usage:
    import chalkcompute

    @chalkcompute.cls()
    class Adder:
        @chalkcompute.before()
        def on_startup(self) -> None:
            print("Adder started")

        @chalkcompute.method()
        def add(self, x: int, y: int) -> int:
            return x + y

        @chalkcompute.after()
        def on_shutdown(self) -> None:
            print("Adder stopped")

    result = Adder.add(1, 2)
    print(result)
    Adder.delete()

Or imperatively:

    from chalkcompute import RemoteClass

    class Adder:
        @chalkcompute.method()
        def add(self, x: int, y: int) -> int:
            return x + y

    rc = RemoteClass(Adder)
    rc.deploy().wait_ready()
    print(rc.add(1, 2))
    rc.delete()
"""

from __future__ import annotations

import ast
import inspect
import logging
import os
import time
import typing
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from chalkcompute._connect import ConnectClient
from chalkcompute._tracing import traced
from chalkcompute.function import (
    _CATALOG_SVC,
    _REMOTE_ENV_VAR,
    _SG_SVC,
    FunctionVersionInfo,
    RemoteFunction,
    _build_and_upload,
    _cleanup_temp_files,
    _extract_field_names,
    _local_install_entrypoint,
    _make_base_image,
    _make_container_spec,
    _should_strip_source,
    _write_temp,
)

if TYPE_CHECKING:
    from chalkcompute.image import Image as _Image

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Marker attribute names
# ---------------------------------------------------------------------------

_CHALK_METHOD_MARKER = "_chalk_method"
_CHALK_BEFORE_MARKER = "_chalk_before"
_CHALK_AFTER_MARKER = "_chalk_after"

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


def method() -> Callable:
    """Mark a class method for remote deployment."""

    def decorator(func: Callable) -> Callable:
        setattr(func, _CHALK_METHOD_MARKER, True)
        return func

    return decorator


def before() -> Callable:
    """Mark a method as the startup hook (called before the server starts)."""

    def decorator(func: Callable) -> Callable:
        setattr(func, _CHALK_BEFORE_MARKER, True)
        return func

    return decorator


def after() -> Callable:
    """Mark a method as the shutdown hook (called when the process exits)."""

    def decorator(func: Callable) -> Callable:
        setattr(func, _CHALK_AFTER_MARKER, True)
        return func

    return decorator


# ---------------------------------------------------------------------------
# Internal data
# ---------------------------------------------------------------------------


@dataclass
class _MethodInfo:
    name: str
    func: Callable
    remote_function: RemoteFunction | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Source transformation
# ---------------------------------------------------------------------------


def _strip_chalkcompute_from_class_source(source: str) -> str:
    """Remove chalkcompute imports, decorators, and lifecycle calls.

    Like function._strip_chalkcompute_from_source but uses ast.walk() to
    catch decorators inside ClassDef bodies (not just top-level nodes).
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)

    remove_ranges: list[tuple[int, int]] = []
    decorated_names: set[str] = set()

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
                if dec_source and "chalkcompute" in dec_source:
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

    Like function._build_schemas but skips the ``self`` parameter.
    """
    from chalkcompute.arrow_schema import rich_to_arrow_schema

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
    """Return True if *func*'s first parameter is ``self``."""
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
) -> str:
    """Generate a handler shim for a single method on a class.

    The shim instantiates the class, wires up lifecycle hooks, and
    delegates Arrow columnar data to the target method.  Methods that
    lack a ``self`` parameter are called as static/plain functions via
    the class rather than the instance.
    """
    parts: list[str] = [
        "import pyarrow as pa",
    ]

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
    parts += [
        "",
        "def handler(event, context):",
        "    n = len(next(iter(event.values())))",
        "    results = []",
        "    for i in range(n):",
        "        kwargs = {k: v[i].as_py() for k, v in event.items()}",
        f"        results.append({method_caller}.{method_name}(**kwargs))",
        "    return pa.array(results)",
    ]

    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# RemoteClass
# ---------------------------------------------------------------------------


class RemoteClass:
    """A managed class backed by ExternalFunctionCatalogService.

    Deploys each ``@method()`` as a separate function version with its own
    scaling group, sharing a single container image.  Lifecycle hooks
    (``@before()`` / ``@after()``) run in every method's container.
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
        min_replicas: int = 1,
        max_replicas: int = 1,
        volumes: list[tuple[str, str]] | None = None,
    ) -> None:
        self._user_cls = user_cls
        self._class_name = user_cls.__name__
        self._explicit_name = name
        self._image = image
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        self._env = env or {}
        self._min_replicas = min_replicas
        self._max_replicas = max_replicas
        self._volumes: list[tuple[str, str]] = list(volumes or [])

        self._client = ConnectClient()
        self._deployed = False

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
                    raise ClassError("Only one @before() method is allowed per class")
                self._before_name = attr_name
            if getattr(attr, _CHALK_AFTER_MARKER, False):
                if self._after_name is not None:
                    raise ClassError("Only one @after() method is allowed per class")
                self._after_name = attr_name

        if not self._methods:
            raise ClassError(
                f"Class {user_cls.__name__} has no @method() decorated methods"
            )

    # -- Alternate constructors ------------------------------------------------

    @classmethod
    def from_name(cls, name: str) -> RemoteClass:
        """Attach to an existing remote class by name.

        Discovers all function versions whose ``functionName`` starts with
        ``{name}.`` and rebuilds a ``RemoteClass`` with callable methods.
        """
        rc = cls._make_empty(name)

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
    def _make_empty(cls, name: str) -> RemoteClass:
        rc = cls.__new__(cls)
        rc._user_cls = None  # type: ignore[assignment]
        rc._class_name = name
        rc._explicit_name = name
        rc._image = None
        rc._cpu = None
        rc._memory = None
        rc._gpu = None
        rc._env = {}
        rc._min_replicas = 1
        rc._max_replicas = 1
        rc._volumes = []
        rc._client = ConnectClient()
        rc._deployed = False
        rc._methods = {}
        rc._before_name = None
        rc._after_name = None
        return rc

    # -- Image building --------------------------------------------------------

    def _build_deploy_image(self) -> tuple[Any, list[str]]:
        """Build the deploy image with all handler shims and user module.

        Returns (Image, list_of_temp_file_paths).
        """
        source_file = inspect.getfile(self._user_cls)

        temp_files: list[str] = []

        if _should_strip_source():
            from pathlib import Path

            source = Path(source_file).read_text()
            stripped = _strip_chalkcompute_from_class_source(source)
            source_file = _write_temp(stripped)
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
            )
            shim_path = _write_temp(shim_content)
            temp_files.append(shim_path)
            deploy_image = deploy_image.add_local_file(
                shim_path, f"/app/handler_{method_info.name}.py"
            )

        deploy_image = deploy_image.env({"PYTHONPATH": "/app", _REMOTE_ENV_VAR: "1"})

        return deploy_image, temp_files

    # -- Lifecycle -------------------------------------------------------------

    @traced("chalkcompute.cls.deploy")
    def deploy(
        self,
        *,
        poll_interval: float = 0.5,
        build_timeout: float = 600.0,
        ready_timeout: float = 300.0,
    ) -> RemoteClass:
        """Build the image and deploy all methods as function versions.

        Returns self for chaining.
        """
        from google.protobuf.json_format import MessageToDict

        # 1. Build deploy image
        deploy_image, temp_files = self._build_deploy_image()

        # 2. Build image + upload lazy files
        image_uri, new_vols = _build_and_upload(
            self._client,
            deploy_image,
            label=f"class '{self._class_name}'",
            volume_prefix="cls-files",
            poll_interval=poll_interval,
            build_timeout=build_timeout,
        )
        self._volumes += new_vols
        _cleanup_temp_files(temp_files)

        # 3. Register each method as a function version
        sg_names: list[str] = []
        for method_info in self._methods.values():
            input_schema, output_schema = _build_method_schemas(method_info.func)

            name_prefix = self._explicit_name or self._class_name
            func_name = f"{name_prefix}.{method_info.name}"
            if self._explicit_name:
                sg_name = f"{self._explicit_name}-{method_info.name}"
            else:
                sg_name = (
                    f"cls-{self._class_name}-{method_info.name}-{uuid.uuid4().hex[:8]}"
                )

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
            spec: dict[str, Any] = {
                "containerSpec": container_spec,
                "scalingSpec": {
                    "minReplicas": self._min_replicas,
                    "maxReplicas": self._max_replicas,
                },
            }

            logger.info("Registering method '%s'...", func_name)
            resp = self._client.rpc(
                _CATALOG_SVC,
                "CreateExternalFunctionVersion",
                {
                    "functionName": func_name,
                    "inputArrowSchema": MessageToDict(input_schema),
                    "outputArrowSchema": MessageToDict(output_schema),
                    "spec": spec,
                },
                timeout=60,
            )
            if resp.status_code != 200:
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
            rf._version_info = version_info
            rf._web_url = web_url
            rf._port = port
            rf._input_field_names = _extract_field_names(efv.get("inputArrowSchema"))
            method_info.remote_function = rf

            logger.info("  %s v%s (id=%s)", func_name, version_info.version, version_info.id)

            if sg_status != "Running":
                sg_names.append(version_info.scaling_group_name)

        # 5. Poll scaling groups until running
        for sg_name in sg_names:
            self._poll_until_running(
                sg_name=sg_name,
                poll_interval=poll_interval,
                timeout=ready_timeout,
            )

        self._deployed = True
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
            if status == "Running":
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

        Returns self for chaining.
        """
        for method_info in self._methods.values():
            if method_info.remote_function is not None:
                method_info.remote_function.wait_ready(
                    timeout=timeout,
                    poll_interval=poll_interval,
                )
        return self

    @traced("chalkcompute.cls.delete")
    def delete(self) -> None:
        """Delete all function versions and clean up volumes."""
        for method_info in self._methods.values():
            if method_info.remote_function is not None:
                method_info.remote_function.delete()
                method_info.remote_function = None

        for vol_name, _ in self._volumes:
            self._client.rpc(
                "chalk.volume.v1.VolumeService",
                "DeleteVolume",
                {"name": vol_name},
            )
        self._volumes = []
        self._deployed = False

    def refresh(self) -> dict[str, FunctionVersionInfo]:
        """Fetch latest info for all methods from the server."""
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
            return mi.remote_function
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")

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
    """Wraps a class so that lifecycle methods are no-ops.

    Returned by the ``cls()`` decorator when running inside the container
    (``_REMOTE_ENV_VAR``), so calls like ``Adder.deploy()`` don't raise.
    """

    def __init__(self, user_cls: type) -> None:
        self._instance = user_cls()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._instance, name)

    def __call__(self) -> _NoOpClass:
        """Behave like a zero-arg constructor in remote handler imports."""
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


def cls(
    *,
    image: _Image | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    gpu: str | None = None,
    name: str | None = None,
    env: dict[str, str] | None = None,
    min_replicas: int = 1,
    max_replicas: int = 1,
    volumes: list[tuple[str, str]] | None = None,
) -> Callable[..., RemoteClass | _NoOpClass]:
    """Decorator that deploys a class as versioned remote functions.

    **Locally** (at decoration time):
    1. Scans the class for ``@method()``, ``@before()``, ``@after()``
    2. Builds a deploy image with per-method handler shims
    3. Registers each method via ExternalFunctionCatalogService
    4. If ``CHALK_FUNCTION_LOCAL_SDK_COPY=1``, includes local SDK source in
       the deploy image for unreleased feature iteration.

    **Remotely** (inside the container):
    The decorator is a no-op — returns an instance of the class.
    """

    def decorator(user_cls: type) -> RemoteClass | _NoOpClass:
        if os.environ.get(_REMOTE_ENV_VAR):
            return _NoOpClass(user_cls)

        rc = RemoteClass(
            user_cls,
            image=image,
            name=name,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            env=env,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            volumes=volumes,
        )
        rc.deploy()
        return rc

    return decorator
