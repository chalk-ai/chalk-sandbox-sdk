"""
Deploy Python functions as versioned remote functions backed by ExternalFunctionCatalogService.

Usage:
    import chalkcompute

    @chalkcompute.function()
    def add(x: int, y: int) -> int:
        return x + y

    result = add(1, 2)

Or imperatively:

    from chalkcompute import RemoteFunction

    def add(x: int, y: int) -> int:
        return x + y

    f = RemoteFunction(add)
    f.deploy().wait_ready()
    print(f(1, 2))
    f.delete()
"""

from __future__ import annotations

import ast
import functools
import inspect
import logging
import os
import tempfile
import time
import typing
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from chalkcompute._connect import (
    ConnectClient,
    build_image,
    grpc_target_from_url,
    upload_lazy_files,
)
from chalkcompute._naming import sanitize_dns_label, validate_dns_label
from chalkcompute._tracing import traced
from chalkcompute._retry import RetryPolicy

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from chalkcompute._image import Image as _Image
    from chalkcompute._secret import Secret as _Secret

SerializationFormat = typing.Literal["pyarrow"]

_REMOTE_ENV_VAR = "_CHALK_REMOTE_FUNCTION"
# When enabled, mount the local SDK project and pip-install at container start.
_LOCAL_COPY_ENV_VAR = "CHALK_FUNCTION_LOCAL_SDK_COPY"
# When enabled, strip chalkcompute references from user source instead of
# mounting the local SDK.  Lighter-weight alternative to _LOCAL_COPY_ENV_VAR.
_STRIP_SOURCE_ENV_VAR = "CHALK_FUNCTION_STRIP_SOURCE"
# When enabled, route __call__ through the API server's CallExternalFunction
# RPC instead of connecting directly to the scaling group's web URL.
_CALL_VIA_API_ENV_VAR = "CHALK_FUNCTION_CALL_VIA_API"
_LOCAL_SDK_MOUNT = "/tmp/sdk"
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

    # Track names of functions decorated with @chalkcompute.function(...)
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

        # Remove @chalkcompute.function(...) decorators from function defs
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                dec_source = ast.get_source_segment(source, dec)
                if dec_source and "chalkcompute" in dec_source:
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


def _generate_handler_shim(func_name: str) -> str:
    """Generate a ``handler.py`` that bridges Arrow columnar data to scalar calls.

    ``chalk-remote-call`` passes ``event`` as ``dict[str, pa.Array]``.
    The shim iterates over rows, extracts Python scalars via ``.as_py()``,
    calls the user function per-row, and returns results.

    If the user function is a generator (yields values), each yielded value
    is returned as a separate item so that ``chalk-remote-call`` can stream
    them as individual response chunks.

    Parameters
    ----------
    func_name
        Name of the user function to import and invoke.

    Returns
    -------
    str
        Python source for the handler shim.
    """
    return (
        "import inspect\n"
        "\n"
        f"from user_module import {func_name}\n"
        "\n"
        f"_is_sync_gen = inspect.isgeneratorfunction({func_name})\n"
        f"_is_async_gen = inspect.isasyncgenfunction({func_name})\n"
        f"_is_coroutine = inspect.iscoroutinefunction({func_name}) and not _is_async_gen\n"
        "\n"
        "\n"
        "def handler(event, context):\n"
        "    n = len(next(iter(event.values())))\n"
        "    if _is_sync_gen:\n"
        "        def _gen():\n"
        "            for i in range(n):\n"
        "                kwargs = {k: v[i].as_py() for k, v in event.items()}\n"
        f"                yield from {func_name}(**kwargs)\n"
        "        return _gen()\n"
        "    elif _is_async_gen:\n"
        "        async def _agen():\n"
        "            for i in range(n):\n"
        "                kwargs = {k: v[i].as_py() for k, v in event.items()}\n"
        f"                async for item in {func_name}(**kwargs):\n"
        "                    yield item\n"
        "        return _agen()\n"
        "    elif _is_coroutine:\n"
        "        import asyncio\n"
        "        results = []\n"
        "        for i in range(n):\n"
        "            kwargs = {k: v[i].as_py() for k, v in event.items()}\n"
        f"            results.append(asyncio.run({func_name}(**kwargs)))\n"
        "        return results\n"
        "    else:\n"
        "        results = []\n"
        "        for i in range(n):\n"
        "            kwargs = {k: v[i].as_py() for k, v in event.items()}\n"
        f"            results.append({func_name}(**kwargs))\n"
        "        return results\n"
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
    if _STRIP_SOURCE_ENV_VAR in os.environ:
        return os.environ[_STRIP_SOURCE_ENV_VAR] == "1"

    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    try:
        return _pkg_version("chalkcompute") == "0.0.0"
    except PackageNotFoundError:
        return False


def _should_call_via_api() -> bool:
    return os.environ.get(_CALL_VIA_API_ENV_VAR, "") == "1"


def _prepare_source_file(source_file: str) -> tuple[str, bool]:
    """Return the source file path to deploy, optionally stripping chalkcompute.

    Parameters
    ----------
    source_file
        Path to the original user source file.

    Returns
    -------
    tuple of (str, bool)
        ``(path, is_temp)`` — if stripping produced a temp file, the caller
        should clean it up.
    """
    if not _should_strip_source():
        return source_file, False
    from pathlib import Path

    source = Path(source_file).read_text()
    stripped = _strip_chalkcompute_from_source(source)
    return _write_temp(stripped), True


# ---------------------------------------------------------------------------
# Shared image / deploy helpers (used by both function.py and cls.py)
# ---------------------------------------------------------------------------


def _local_install_entrypoint(handler_cmd: list[str]) -> list[str]:
    """Wrap ``handler_cmd`` with a tar extract + pip install when local-copy mode is on.

    Parameters
    ----------
    handler_cmd
        Original handler command to wrap.

    Returns
    -------
    list of str
        Possibly-wrapped entrypoint command.
    """
    strip_source = _should_strip_source()
    if os.environ.get(_LOCAL_COPY_ENV_VAR, "") != "1" or strip_source:
        return handler_cmd
    from chalkcompute._image import ARCHIVE_NAME, _ARCHIVE_CHUNK_PREFIX

    d = _LOCAL_SDK_MOUNT
    # Reassemble chunked archive, extract, then pip install.
    cat_parts = f"cat {d}/{_ARCHIVE_CHUNK_PREFIX}* > {d}/{ARCHIVE_NAME}"
    extract = f"tar xzf {d}/{ARCHIVE_NAME} -C {d}"
    install = f"pip install {d}"
    return [
        "sh",
        "-c",
        f"{cat_parts} && {extract} && {install} && {' '.join(handler_cmd)}",
    ]


def _make_base_image(user_image: _Image | None) -> _Image:
    """Create the base deploy image with SDK packages and optional local overlay."""
    from chalkcompute._image import Image

    strip_source = _should_strip_source()
    local_copy = os.environ.get(_LOCAL_COPY_ENV_VAR, "") == "1" and not strip_source

    base = user_image if user_image is not None else Image.debian_slim()

    if local_copy:
        # Mount the local SDK project via volume; the entrypoint will
        # extract the tar and pip-install before starting the handler.
        project_root = Path(__file__).resolve().parent.parent
        image = base.add_local_dir(
            str(project_root),
            _LOCAL_SDK_MOUNT,
            exclude=["tests", "e2etests", "examples"],
            archive=True,
        )
    elif strip_source:
        # Source is stripped of chalkcompute references — only install the
        # RPC runtime, not chalkcompute itself.
        image = base.pip_install(["chalk-remote-call-python"])
    else:
        from importlib.metadata import version as _pkg_version

        image = base.pip_install(
            [
                "chalk-remote-call-python",
                f"chalkcompute=={_pkg_version('chalkcompute')}",
            ]
        )

    return image


def _build_and_upload(
    client: ConnectClient,
    deploy_image: _Image,
    *,
    label: str,
    volume_prefix: str,
    poll_interval: float,
    build_timeout: float,
    progress_parent: str | None = None,
) -> tuple[str, list[tuple[str, str]]]:
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
    tuple of (str, list of tuple of (str, str))
        ``(image_uri, new_volumes)``.
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

    new_volumes: list[tuple[str, str]] = []
    if deploy_image.lazy_local_files:
        vkey = f"upload:{label}"
        progress_add(
            vkey, "volume",
            f"Uploading {len(deploy_image.lazy_local_files)} file(s)",
            parent=progress_parent,
        )
        logger.info("Uploading %d local file(s)...", len(deploy_image.lazy_local_files))
        new_volumes = upload_lazy_files(
            client, deploy_image, volume_prefix=volume_prefix
        )
        progress_done(vkey)
    return image_uri, new_volumes


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
    volumes: list[tuple[str, str]],
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
        List of ``(volume_name, mount_path)`` tuples.

    Returns
    -------
    dict
        Container spec dictionary.
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
        spec["volumes"] = [
            {"name": vn, "mountPath": mp, "type": "chalkfs"} for vn, mp in volumes
        ]
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
    >>> img = (
    ...     Image.debian_slim()
    ...     .pip_install(["numpy"])
    ... )
    >>> @chalkcompute.function(
    ...     image=img,
    ...     cpu="1",
    ...     memory="2Gi",
    ...     min_replicas=1,
    ...     max_replicas=4,
    ... )
    ... def embed(text: str) -> list[float]:
    ...     import numpy as np
    ...     return np.ones(8).tolist()
    >>> embed.deploy()
    >>> embed("hello world")
    [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]

    Imperative form, for constructing a ``RemoteFunction`` from an existing
    callable:

    >>> from chalkcompute import RemoteFunction, Image
    >>> def square(x: int) -> int:
    ...     return x * x
    >>> fn = RemoteFunction(
    ...     square,
    ...     image=Image.debian_slim(),
    ...     name="square",
    ...     min_replicas=1,
    ...     max_replicas=2,
    ... )
    >>> fn.deploy()
    >>> fn(7)
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
        min_replicas: int = 1,
        max_replicas: int = 1,
        volumes: list[tuple[str, str]] | None = None,
        secrets: list[_Secret] | None = None,
        serialization_format: SerializationFormat = "pyarrow",
        suspend_cooldown_period: int | None = None,
        options: dict[str, str] | None = None,
        max_buffer_duration: int | None = None,
        max_batching_size: int | None = None,
        retries: int | RetryPolicy | None = None,
        rate_limit_key: str | None = None,
    ) -> None:
        self._func = func
        if name is not None:
            validate_dns_label(name)
        self._explicit_name = name
        self._func_name = name or sanitize_dns_label(
            getattr(func, "__name__", "anonymous")
        )
        self._image = image
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        self._env = env or {}
        self._min_replicas = min_replicas
        self._max_replicas = max_replicas
        self._volumes: list[tuple[str, str]] = list(volumes or [])
        self._secrets: list[_Secret] = list(secrets or [])
        self._serialization_format: SerializationFormat = serialization_format
        self._suspend_cooldown_period = suspend_cooldown_period
        self._options = options or {}
        self._max_buffer_duration = max_buffer_duration
        self._max_batching_size = max_batching_size
        self._retries = RetryPolicy.from_int_or_policy(retries) if retries is not None else None
        self._rate_limit_key = rate_limit_key

        # Set after .deploy()
        self._version_info: FunctionVersionInfo | None = None
        self._web_url: str | None = None
        self._port: int = 8080

        # Input field names (populated from API response for from_id/from_name)
        self._input_field_names: list[str] = []

        # gRPC call channel (lazily created on first __call__)
        self._call_channel: Any = None
        self._call_stub: Any = None

        self._client = ConnectClient()

        functools.update_wrapper(self, func)

    # -- Alternate constructors ------------------------------------------------

    @classmethod
    def from_id(cls, version_id: str) -> RemoteFunction:
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
        f = cls._make_empty()
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
        f._input_field_names = _extract_field_names(efv.get("inputArrowSchema"))
        sg = data.get("scalingGroup")
        if sg:
            f._web_url = sg.get("webUrl")
            f._port = sg.get("port", 8080)
        return f

    @classmethod
    def from_name(cls, name: str) -> RemoteFunction:
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
        f = cls._make_empty()
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
        f._explicit_name = name
        f._input_field_names = _extract_field_names(efv.get("inputArrowSchema"))
        sg = data.get("scalingGroup")
        if sg:
            f._web_url = sg.get("webUrl")
            f._port = sg.get("port", 8080)
        return f

    @classmethod
    def _make_empty(cls) -> RemoteFunction:
        f = cls.__new__(cls)
        f._func = lambda *a, **kw: None  # type: ignore[assignment]
        f._explicit_name = None
        f._func_name = "unknown"
        f._image = None
        f._cpu = None
        f._memory = None
        f._gpu = None
        f._env = {}
        f._min_replicas = 1
        f._max_replicas = 1
        f._volumes = []
        f._secrets = []
        f._serialization_format = "pyarrow"
        f._suspend_cooldown_period = None
        f._options = {}
        f._max_buffer_duration = None
        f._max_batching_size = None
        f._retries = None
        f._rate_limit_key = None
        f._version_info = None
        f._web_url = None
        f._port = 6666
        f._input_field_names = []
        f._call_channel = None
        f._call_stub = None
        f._client = ConnectClient()
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
            inspect.getfile(self._func)
        )

        handler_shim = _generate_handler_shim(
            getattr(self._func, "__name__", self._func_name)
        )
        shim_path = _write_temp(handler_shim)

        deploy_image = (
            _make_base_image(self._image)
            .add_local_file(source_file, "/app/user_module.py")
            .add_local_file(shim_path, "/app/handler.py")
            .env({"PYTHONPATH": "/app", _REMOTE_ENV_VAR: "1"})
        )

        self._temp_files = [shim_path] + ([source_file] if source_is_temp else [])
        return deploy_image

    # -- Lifecycle -------------------------------------------------------------

    @traced("chalkcompute.function.deploy")
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
        progress_add(fkey, "function", f"Function \"{self._func_name}\"")

        try:
            # 1. Build deploy image
            deploy_image = self._build_deploy_image()

            # 2. Build image + upload lazy files
            image_uri, new_vols = _build_and_upload(
                self._client,
                deploy_image,
                label=f"function '{self._func_name}'",
                volume_prefix="fn-files",
                poll_interval=poll_interval,
                build_timeout=build_timeout,
                progress_parent=fkey,
            )
            self._volumes += new_vols
            _cleanup_temp_files(getattr(self, "_temp_files", []))
            self._temp_files = []

            # 3. Generate schemas from function signature
            input_schema_proto, output_schema_proto = _build_schemas(self._func)

            # 4. Build ScalingGroupSpec
            sg_name = sanitize_dns_label(
                self._explicit_name
                if self._explicit_name
                else f"fn-{self._func_name}-{uuid.uuid4().hex[:8]}"
            )
            handler_cmd = ["chalk-remote-call", "--handler", "handler.handler"]
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

            # 5b. Resolve lazy secrets and add to container spec
            if self._secrets:
                from chalkcompute._secret import resolve_lazy_secrets

                if any(s.is_lazy for s in self._secrets):
                    resolved, _upserted = resolve_lazy_secrets(self._secrets, self._client)
                    self._secrets = resolved
                container_spec["secretRefs"] = [s._to_proto_dict() for s in self._secrets]

            # 5c. Build function-level config
            function_config: dict[str, Any] = {}
            if self._serialization_format != "pyarrow":
                function_config["serializationFormat"] = self._serialization_format
            if self._suspend_cooldown_period is not None:
                function_config["suspendCooldownPeriod"] = self._suspend_cooldown_period
            if self._options:
                function_config["options"] = self._options
            if self._max_buffer_duration is not None:
                function_config["maxBufferDuration"] = self._max_buffer_duration
            if self._max_batching_size is not None:
                function_config["maxBatchingSize"] = self._max_batching_size
            if self._retries is not None:
                function_config["retryPolicy"] = self._retries.to_dict()
            if self._rate_limit_key is not None:
                function_config["rateLimitKey"] = self._rate_limit_key

            # 6. Call CreateExternalFunctionVersion
            rkey = f"register:{self._func_name}"
            progress_add(rkey, "function", "Registering function", parent=fkey)
            logger.info("Registering function '%s'...", self._func_name)

            create_request: dict[str, Any] = {
                "functionName": self._func_name,
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
                raise FunctionError(f"CreateExternalFunctionVersion failed: {resp.text}")

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
            self._web_url = sg.get("webUrl")
            logger.info("  poll: status=%s", status)
            if status in ("Running", "Available"):
                return
            if status in ("Failed", "Error"):
                raise FunctionError(f"Scaling group {sg_name} entered {status} state")
            time.sleep(poll_interval)
        raise FunctionError(f"Scaling group {sg_name} timed out after {timeout}s")

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

        # Ensure we have auth tokens for gRPC metadata
        self._client._ensure_auth()
        self._call_metadata = [
            ("authorization", f"Bearer {self._client._access_token}"),
            ("x-chalk-env-id", self._client._env_id or ""),
        ]

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
                "name": "handler",
                "featherStream": base64.b64encode(feather_bytes).decode("ascii"),
            },
        }

        resp = self._client.rpc(
            _CATALOG_SVC,
            "CallExternalFunction",
            request_body,
            timeout=300,
        )
        if resp.status_code != 200:
            raise FunctionError(f"CallExternalFunction failed: {resp.text}")

        data = resp.json()
        remote_resp = data.get("remoteCallResponse")
        if not remote_resp:
            raise FunctionError("No remoteCallResponse in CallExternalFunction response")

        feather_b64 = remote_resp.get("featherStream")
        if not feather_b64:
            raise FunctionError("No featherStream in CallExternalFunction response")

        return base64.b64decode(feather_b64)

    @traced("chalkcompute.function.call")
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Call the remote function via gRPC or the API server.

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
        """
        import pyarrow as pa
        import pyarrow.ipc as ipc

        # 1. Build name→value mapping from args
        if self._input_field_names:
            # from_id / from_name: use schema field names
            arguments: dict[str, Any] = {}
            for i, name in enumerate(self._input_field_names):
                if name in kwargs:
                    arguments[name] = kwargs[name]
                elif i < len(args):
                    arguments[name] = args[i]
                else:
                    raise TypeError(f"Missing argument: {name}")
        else:
            # Normal constructor: use function signature
            sig = inspect.signature(self._func)
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = bound.arguments

        # 2. Build RecordBatch from args (one column per param, single row)
        arrays = []
        names = []
        for param_name, value in arguments.items():
            arrays.append(pa.array([value]))
            names.append(param_name)
        batch = pa.record_batch(arrays, names=names)

        # 3. Serialize to Arrow IPC (Feather) bytes
        sink = pa.BufferOutputStream()
        with ipc.new_stream(sink, batch.schema) as writer:
            writer.write_batch(batch)
        feather_bytes = sink.getvalue().to_pybytes()

        # 4. Call: branch on routing mode
        if _should_call_via_api():
            response_bytes = self._call_via_api_rpc(feather_bytes)
            results: list[Any] = []
            reader = ipc.open_stream(response_bytes)
            for result_batch in reader:
                result_dict = result_batch.to_pydict()
                if "result" in result_dict:
                    results.extend(result_dict["result"])
        else:
            from chalk_remote_call._gen.chalk.runtime.v1 import (
                remote_python_call_pb2 as call_pb,
            )

            self._ensure_call_stub()

            def requests():
                yield call_pb.CallFunctionRequest(  # ty: ignore[unresolved-attribute]
                    name="handler",
                    feather_stream=feather_bytes,
                )

            responses = list(
                self._call_stub.CallFunction(
                    requests(), metadata=self._call_metadata
                )
            )

            if not responses:
                raise FunctionError("No response received from remote function")

            results = []
            for resp in responses:
                reader = ipc.open_stream(resp.feather_stream)
                for result_batch in reader:
                    result_dict = result_batch.to_pydict()
                    if "result" in result_dict:
                        results.extend(result_dict["result"])

        # 5. Single-row call returns scalar
        if len(results) == 1:
            return results[0]
        return results

    # -- Management ------------------------------------------------------------

    @traced("chalkcompute.function.delete")
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
    """Wraps a function so that lifecycle methods are no-ops.

    Returned by the decorator when running inside the container
    (_REMOTE_ENV_VAR), so calls like ``add.wait_ready()`` don't
    raise AttributeError.
    """

    def __init__(self, func: Callable) -> None:
        self._func = func
        functools.update_wrapper(self, func)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._func(*args, **kwargs)

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


def function(
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
    secrets: list[_Secret] | None = None,
    serialization_format: SerializationFormat = "pyarrow",
    suspend_cooldown_period: int | None = None,
    options: dict[str, str] | None = None,
    max_buffer_duration: int | None = None,
    max_batching_size: int | None = None,
    retries: int | RetryPolicy | None = None,
    rate_limit_key: str | None = None,
) -> Callable[..., RemoteFunction | _NoOpFunction]:
    """Decorator that deploys a function as a versioned remote function.

    **Locally** (at decoration time):

    1. Derives Arrow schemas from the function's type hints
    2. Builds a deploy image with a handler shim
    3. Registers the function version via ``ExternalFunctionCatalogService``
    4. If ``CHALK_FUNCTION_LOCAL_SDK_COPY=1``, includes local SDK source in
       the deploy image for unreleased feature iteration.

    **Remotely** (inside the container):
    The decorator is a no-op — returns the raw function unchanged.

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
    min_replicas
        Minimum number of replicas.
    max_replicas
        Maximum number of replicas.
    volumes
        List of ``(name, mount_path)`` tuples for persistent storage.
    secrets
        List of :class:`~chalkcompute.Secret` references to inject.
    serialization_format
        Wire format for arguments and results. Currently only ``"pyarrow"``
        is supported.
    suspend_cooldown_period
        Time in seconds until containers shut down after the last request.
    options
        Arbitrary key-value options forwarded to the Chalk platform.
    max_buffer_duration
        Maximum time in milliseconds to buffer incoming items before invoking
        the handler. Defaults to 1000 ms when batching is enabled. When
        batching is enabled, handler args are lists or a single pyarrow table.
    max_batching_size
        Maximum number of items to accumulate before invoking the handler.
        Defaults to 10 when batching is enabled.
    retries
        Retry policy for handler invocations. Pass an ``int`` for simple
        max-attempts with default exponential backoff, or a
        :class:`~chalk_remote_call.config.RetryPolicy` for full control.
    rate_limit_key
        Name of an externally defined rate limit policy. Used to control
        outbound concurrency from multiple sources.

    Examples
    --------
    >>> import chalkcompute
    >>> @chalkcompute.function()
    ... def add(x: int, y: int) -> int:
    ...     return x + y
    >>> add(1, 2)
    """

    def decorator(func: Callable) -> RemoteFunction | _NoOpFunction:
        if os.environ.get(_REMOTE_ENV_VAR):
            return _NoOpFunction(func)

        f = RemoteFunction(
            func,
            image=image,
            name=name,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            env=env,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            volumes=volumes,
            secrets=secrets,
            serialization_format=serialization_format,
            suspend_cooldown_period=suspend_cooldown_period,
            options=options,
            max_buffer_duration=max_buffer_duration,
            max_batching_size=max_batching_size,
            retries=retries,
            rate_limit_key=rate_limit_key,
        )
        f.deploy()
        return f

    return decorator
