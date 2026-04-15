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

from chalkcompute._connect import ConnectClient, build_image, upload_lazy_files
from chalkcompute._naming import sanitize_dns_label, validate_dns_label
from chalkcompute._tracing import traced

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from chalkcompute.image import Image as _Image

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
    function_name: str
    version: int
    scaling_group_name: str


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
    """Generate a handler.py that bridges Arrow columnar data to scalar calls.

    chalk-remote-call passes ``event`` as ``dict[str, pa.Array]``.
    The shim iterates over rows, extracts Python scalars via ``.as_py()``,
    calls the user function per-row, and returns a ``pa.array`` of results.
    """
    return (
        "import pyarrow as pa\n"
        "\n"
        f"from user_module import {func_name}\n"
        "\n"
        "\n"
        "def handler(event, context):\n"
        "    n = len(next(iter(event.values())))\n"
        "    results = []\n"
        "    for i in range(n):\n"
        "        kwargs = {k: v[i].as_py() for k, v in event.items()}\n"
        f"        results.append({func_name}(**kwargs))\n"
        "    return pa.array(results)\n"
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

    Returns (path, is_temp) — if stripping produced a temp file, the caller
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
    """Wrap *handler_cmd* with a tar extract + pip install when local-copy mode is on."""
    strip_source = _should_strip_source()
    if os.environ.get(_LOCAL_COPY_ENV_VAR, "") != "1" or strip_source:
        return handler_cmd
    from chalkcompute.image import ARCHIVE_NAME, _ARCHIVE_CHUNK_PREFIX

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
    from chalkcompute.image import Image

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
            exclude=[".git", "__pycache__", "*.pyc", ".env*", "e2etests", "examples"],
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

    Returns (image_uri, new_volumes).
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
    """Remove temporary files, ignoring errors."""
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
    """Build a container spec dict for CreateExternalFunctionVersion."""
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
    """Derive input/output arrow_pb2.Schema protos from function type hints.

    Goes directly from Python type annotations to arrow_pb2.Schema protos
    without an intermediate PyArrow representation.
    """
    from chalkcompute.arrow_schema import rich_to_arrow_schema

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
    return_type = hints.get("return")
    if return_type is None:
        raise TypeError("Function must have a return type annotation")
    output_schema = rich_to_arrow_schema({"result": return_type})

    return input_schema, output_schema


def _extract_field_names(schema_dict: dict | None) -> list[str]:
    """Extract field names from an Arrow schema JSON dict."""
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
        """Attach to an existing function version by ID."""
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
        """Attach to an existing function version by name."""
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
        """Build the deploy image with handler shim and user module."""
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

        Returns self for chaining.
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

            # 6. Call CreateExternalFunctionVersion
            rkey = f"register:{self._func_name}"
            progress_add(rkey, "function", "Registering function", parent=fkey)
            logger.info("Registering function '%s'...", self._func_name)
            resp = self._client.rpc(
                _CATALOG_SVC,
                "CreateExternalFunctionVersion",
                {
                    "functionName": self._func_name,
                    "inputArrowSchema": MessageToDict(input_schema_proto),
                    "outputArrowSchema": MessageToDict(output_schema_proto),
                    "spec": spec,
                },
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

        Returns self for chaining.
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
        """Lazily create the gRPC channel and stub for calling the remote function."""
        if self._call_stub is not None:
            return

        import re

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

        target = re.sub(r"^https?://", "", self._web_url).rstrip("/")
        # gRPC requires host:port — use the scaling group's port
        host = target.split("/")[0]
        if ":" not in host:
            if self._web_url.startswith("https"):
                target = f"{host}:443"
            else:
                target = f"{host}:{self._port}"
        else:
            target = host

        if self._web_url.startswith("https"):
            self._call_channel = grpc.secure_channel(
                target, grpc.ssl_channel_credentials()
            )
        else:
            self._call_channel = grpc.insecure_channel(target)
        self._call_stub = call_grpc.RemoteCallServiceStub(self._call_channel)

    def _call_via_api_rpc(self, feather_bytes: bytes) -> bytes:
        """Call the remote function via the API server's CallExternalFunction RPC.

        Returns the response Arrow IPC bytes.
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
        """Call the remote function via gRPC or the API server."""
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
        """Fetch latest function version info from the server."""
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
) -> Callable[..., RemoteFunction | _NoOpFunction]:
    """Decorator that deploys a function as a versioned remote function.

    **Locally** (at decoration time):
    1. Derives Arrow schemas from the function's type hints
    2. Builds a deploy image with a handler shim
    3. Registers the function version via ExternalFunctionCatalogService
    4. If ``CHALK_FUNCTION_LOCAL_SDK_COPY=1``, includes local SDK source in
       the deploy image for unreleased feature iteration.

    **Remotely** (inside the container):
    The decorator is a no-op — returns the raw function unchanged.
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
        )
        f.deploy()
        return f

    return decorator
