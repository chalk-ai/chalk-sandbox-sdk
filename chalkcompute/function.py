"""
Deploy Python functions as versioned remote functions backed by ExternalFunctionCatalogService.

Usage:
    import chalkcompute

    @chalkcompute.function()
    def add(x: int, y: int) -> int:
        return x + y

    result = add(1, 2)

Or imperatively:

    from chalkcompute import Function

    def add(x: int, y: int) -> int:
        return x + y

    f = Function(add)
    f.deploy()
    print(f(1, 2))
    f.delete()
"""

from __future__ import annotations

import ast
import base64
import functools
import inspect
import os
import tempfile
import time
import typing
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import httpx

    from chalkcompute.image import Image as _Image

_REMOTE_ENV_VAR = "_CHALK_REMOTE_FUNCTION"
_STRIP_SOURCE_ENV_VAR = "CHALK_FUNCTION_STRIP_SOURCE"


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
# Source transformation helpers
# ---------------------------------------------------------------------------


def _strip_chalkcompute_from_source(source: str) -> str:
    """Remove chalkcompute imports and @chalkcompute.function decorators.

    Preserves all other code (helpers, constants, classes, non-chalkcompute
    imports) so the deployed module is self-contained minus the SDK.
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)

    # Collect line ranges to remove (1-indexed, inclusive)
    remove_ranges: list[tuple[int, int]] = []

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


# ---------------------------------------------------------------------------
# Function
# ---------------------------------------------------------------------------


class Function:
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
    ) -> None:
        self._func = func
        self._func_name = name or getattr(func, "__name__", "anonymous")
        self._image = image
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        self._env = env or {}
        self._min_replicas = min_replicas
        self._max_replicas = max_replicas

        # Set after .deploy()
        self._version_info: FunctionVersionInfo | None = None
        self._web_url: str | None = None

        # gRPC call channel (lazily created on first __call__)
        self._call_channel: Any = None
        self._call_stub: Any = None

        # Auth state (lazily populated)
        self._api_server: str | None = None
        self._access_token: str | None = None
        self._env_id: str | None = None
        self._connect_headers: dict[str, str] | None = None

        functools.update_wrapper(self, func)

    # -- Auth ------------------------------------------------------------------

    def _ensure_auth(self) -> None:
        if self._access_token is not None:
            return

        import httpx

        self._api_server = os.environ.get("CHALK_API_SERVER", "")
        client_id = os.environ.get("CHALK_CLIENT_ID", "")
        client_secret = os.environ.get("CHALK_CLIENT_SECRET", "")
        self._env_id = os.environ.get("CHALK_ENVIRONMENT_ID", "")

        if not self._api_server or not client_id:
            raise FunctionError(
                "CHALK_API_SERVER and CHALK_CLIENT_ID must be set in the environment"
            )

        resp = httpx.post(
            f"{self._api_server}/v1/oauth/token",
            json={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]

        self._connect_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._access_token}",
            "X-Chalk-Env-Id": self._env_id or "",
        }

    # -- Connect-protocol helpers ----------------------------------------------

    def _connect_rpc(
        self, service: str, method: str, body: dict, timeout: float = 30,
    ) -> httpx.Response:
        import httpx

        assert self._api_server is not None
        assert self._connect_headers is not None
        return httpx.post(
            f"{self._api_server}/{service}/{method}",
            json=body,
            headers=self._connect_headers,
            timeout=timeout,
        )

    def _catalog_rpc(
        self, method: str, body: dict, timeout: float = 30,
    ) -> httpx.Response:
        return self._connect_rpc(
            "chalk.externalfunctioncatalog.v1.ExternalFunctionCatalogService",
            method,
            body,
            timeout,
        )

    def _custom_image_rpc(
        self, method: str, body: dict, timeout: float = 30,
    ) -> httpx.Response:
        return self._connect_rpc(
            "chalk.sandbox.v1.CustomImageService",
            method,
            body,
            timeout,
        )

    def _volume_rpc(
        self, method: str, body: dict, timeout: float = 30,
    ) -> httpx.Response:
        return self._connect_rpc(
            "chalk.volume.v1.VolumeService",
            method,
            body,
            timeout,
        )

    def _scaling_group_rpc(
        self, method: str, body: dict, timeout: float = 30,
    ) -> httpx.Response:
        return self._connect_rpc(
            "chalk.scalinggroup.v1.ScalingGroupManagerService",
            method,
            body,
            timeout,
        )

    # -- Image building --------------------------------------------------------

    def _build_deploy_image(self) -> _Image:
        """Build the deploy image with handler shim and user module."""
        from chalkcompute.image import Image

        base_image = self._image if self._image is not None else Image.debian_slim()
        strip_source = os.environ.get(_STRIP_SOURCE_ENV_VAR, "") == "1"

        source_file = inspect.getfile(self._func)
        if strip_source:
            source = Path(source_file).read_text()
            user_module_path = _write_temp(_strip_chalkcompute_from_source(source))
            extra_packages: list[str] = []
        else:
            user_module_path = source_file
            extra_packages = ["chalkcompute"]

        handler_shim = _generate_handler_shim(getattr(self._func, "__name__", self._func_name))
        shim_path = _write_temp(handler_shim)

        deploy_image = (
            base_image.pip_install(["chalk-remote-call-python", *extra_packages])
            .add_local_file(user_module_path, "/app/user_module.py")
            .add_local_file(shim_path, "/app/handler.py")
            .env({"PYTHONPATH": "/app", _REMOTE_ENV_VAR: "1"})
            .entrypoint(["chalk-remote-call", "--handler", "handler.handler"])
        )

        # Clean up temp files after image build has consumed them
        os.unlink(shim_path)
        if strip_source:
            os.unlink(user_module_path)

        return deploy_image

    def _build_image_uri(
        self, deploy_image: _Image, *, poll_interval: float = 5.0, timeout: float = 600.0,
    ) -> str:
        """Build the Image spec via CustomImageService and return the image URI."""
        from google.protobuf.json_format import MessageToDict

        spec_dict = MessageToDict(deploy_image.to_proto())
        resp = self._custom_image_rpc(
            "GetOrBuildCustomImage",
            {"imageSpec": spec_dict},
        )
        if resp.status_code != 200:
            raise FunctionBuildError(f"GetOrBuildCustomImage failed: {resp.text}")

        data = resp.json()
        status = data.get("status", "")
        if status in ("exists", "succeeded"):
            return data["image"]
        if status == "failed":
            raise FunctionBuildError(f"Image build failed: {data.get('error', 'unknown')}")

        build_id = data["buildId"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            get_resp = self._custom_image_rpc("GetCustomImage", {"buildId": build_id})
            if get_resp.status_code != 200:
                raise FunctionBuildError(f"GetCustomImage failed: {get_resp.text}")
            get_data = get_resp.json()
            status = get_data.get("status", "")
            if status == "succeeded":
                return get_data["image"]
            if status == "failed":
                raise FunctionBuildError(
                    f"Image build {build_id} failed: {get_data.get('error', 'unknown')}"
                )
            time.sleep(poll_interval)

        raise FunctionBuildError(f"Image build {build_id} timed out after {timeout}s")

    def _upload_lazy_files(self, deploy_image: _Image) -> list[tuple[str, str]]:
        """Upload lazy local files to volumes. Returns [(volume_name, mount_path)]."""
        try:
            dir_to_files = deploy_image.lazy_local_files_grouped_for_volume_mounts()
        except ValueError as e:
            raise FunctionError(str(e)) from e

        volumes: list[tuple[str, str]] = []
        for dest_dir, files in dir_to_files.items():
            vol_name = f"fn-files-{uuid.uuid4().hex[:12]}"
            resp = self._volume_rpc("CreateVolume", {"name": vol_name})
            if resp.status_code != 200:
                raise FunctionError(f"CreateVolume failed: {resp.text}")

            for lf in files:
                content = Path(lf.src).read_bytes()
                encoded = base64.b64encode(content).decode("ascii")
                flat_name = Path(lf.dest).name
                put_resp = self._volume_rpc(
                    "PutFile",
                    {"volumeName": vol_name, "path": flat_name, "data": encoded},
                )
                if put_resp.status_code != 200:
                    raise FunctionError(f"PutFile failed for {lf.dest}: {put_resp.text}")

            volumes.append((vol_name, dest_dir))
        return volumes

    # -- Lifecycle -------------------------------------------------------------

    def deploy(
        self,
        *,
        poll_interval: float = 5.0,
        build_timeout: float = 600.0,
        ready_timeout: float = 300.0,
    ) -> Function:
        """Build the image, register the function version, and wait until running.

        Returns self for chaining.
        """
        from google.protobuf.json_format import MessageToDict

        self._ensure_auth()

        # 1. Build deploy image
        deploy_image = self._build_deploy_image()

        # 2. Build image URI
        print(f"Building image for function '{self._func_name}'...")
        image_uri = self._build_image_uri(
            deploy_image, poll_interval=poll_interval, timeout=build_timeout,
        )
        print(f"Image built: {image_uri}")

        # 3. Upload lazy local files
        volumes: list[tuple[str, str]] = []
        if deploy_image.lazy_local_files:
            print(f"Uploading {len(deploy_image.lazy_local_files)} local file(s)...")
            volumes = self._upload_lazy_files(deploy_image)

        # 4. Generate schemas from function signature
        input_schema_proto, output_schema_proto = _build_schemas(self._func)

        # 5. Build ScalingGroupSpec
        sg_name = f"fn-{self._func_name}-{uuid.uuid4().hex[:8]}"
        container_spec: dict[str, Any] = {
            "name": sg_name,
            "image": image_uri,
            "port": 6666,
            "protocol": "grpc",
            "entrypoint": ["chalk-remote-call", "--handler", "handler.handler"],
            "envVars": {
                "PYTHONPATH": "/app",
                _REMOTE_ENV_VAR: "1",
                "CHALK_CLIENT_ID": os.environ.get("CHALK_CLIENT_ID", ""),
                "CHALK_CLIENT_SECRET": os.environ.get("CHALK_CLIENT_SECRET", ""),
                "CHALK_ENVIRONMENT_ID": os.environ.get("CHALK_ENVIRONMENT_ID", ""),
                **self._env,
            },
        }

        resources: dict[str, str] = {}
        if self._cpu:
            resources["cpu"] = self._cpu
        if self._memory:
            resources["memory"] = self._memory
        if self._gpu:
            resources["gpu"] = self._gpu
        if resources:
            container_spec["resources"] = resources

        if volumes:
            container_spec["volumes"] = [
                {"name": vn, "mountPath": mp, "type": "chalkfs"}
                for vn, mp in volumes
            ]

        spec: dict[str, Any] = {
            "containerSpec": container_spec,
            "scalingSpec": {
                "minReplicas": self._min_replicas,
                "maxReplicas": self._max_replicas,
            },
        }

        # 6. Call CreateExternalFunctionVersion
        print(f"Registering function '{self._func_name}'...")
        resp = self._catalog_rpc(
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
            sg_status = sg_data.get("status", "")
        else:
            sg_status = ""

        print(
            f"Function version created: {self._version_info.function_name} "
            f"v{self._version_info.version} (id={self._version_info.id})"
        )

        # 7. Poll scaling group until running
        if sg_status != "Running":
            self._poll_until_running(
                sg_name=self._version_info.scaling_group_name,
                poll_interval=poll_interval,
                timeout=ready_timeout,
            )

        return self

    def _poll_until_running(
        self, *, sg_name: str, poll_interval: float, timeout: float,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            resp = self._scaling_group_rpc(
                "GetScalingGroup", {"name": sg_name},
            )
            if resp.status_code != 200:
                raise FunctionError(f"GetScalingGroup failed: {resp.text}")
            sg = resp.json()["scalingGroup"]
            status = sg.get("status", "")
            self._web_url = sg.get("webUrl")
            print(f"  poll: status={status}")
            if status == "Running":
                return
            if status in ("Failed", "Error"):
                raise FunctionError(
                    f"Scaling group {sg_name} entered {status} state"
                )
        raise FunctionError(
            f"Scaling group {sg_name} timed out after {timeout}s"
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

        target = re.sub(r"^https?://", "", self._web_url)
        if self._web_url.startswith("https"):
            self._call_channel = grpc.secure_channel(target, grpc.ssl_channel_credentials())
        else:
            self._call_channel = grpc.insecure_channel(target)
        self._call_stub = call_grpc.RemoteCallServiceStub(self._call_channel)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Call the remote function via gRPC with Arrow IPC serialization."""
        import pyarrow as pa
        import pyarrow.ipc as ipc
        from chalk_remote_call._gen.chalk.runtime.v1 import (
            remote_python_call_pb2 as call_pb,
        )

        self._ensure_call_stub()

        # 1. Bind args to function signature
        sig = inspect.signature(self._func)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()

        # 2. Build RecordBatch from args (one column per param, single row)
        arrays = []
        names = []
        for param_name, value in bound.arguments.items():
            arrays.append(pa.array([value]))
            names.append(param_name)
        batch = pa.record_batch(arrays, names=names)

        # 3. Serialize to Arrow IPC (Feather) bytes
        sink = pa.BufferOutputStream()
        with ipc.new_stream(sink, batch.schema) as writer:
            writer.write_batch(batch)
        feather_bytes = sink.getvalue().to_pybytes()

        # 4. gRPC streaming call
        def requests():
            yield call_pb.CallFunctionRequest(  # ty: ignore[unresolved-attribute]
                name="handler", feather_stream=feather_bytes,
            )

        responses = list(self._call_stub.CallFunction(requests()))

        if not responses:
            raise FunctionError("No response received from remote function")

        # 5. Deserialize response Arrow IPC bytes
        results: list[Any] = []
        for resp in responses:
            reader = ipc.open_stream(resp.feather_stream)
            for result_batch in reader:
                result_dict = result_batch.to_pydict()
                if "result" in result_dict:
                    results.extend(result_dict["result"])

        # Single-row call returns scalar
        if len(results) == 1:
            return results[0]
        return results

    # -- Management ------------------------------------------------------------

    def delete(self) -> None:
        """Delete this function version."""
        self._ensure_auth()
        if self._version_info is not None:
            self._catalog_rpc(
                "DeleteExternalFunctionVersion",
                {"id": self._version_info.id},
            )
            self._version_info = None
            self._web_url = None

    def refresh(self) -> FunctionVersionInfo:
        """Fetch latest function version info from the server."""
        if self._version_info is None:
            raise FunctionError("No function version — call .deploy() first.")
        self._ensure_auth()
        resp = self._catalog_rpc(
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
        return f"Function({self._func_name!r}{v}, {deployed})"


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
) -> Callable:
    """Decorator that deploys a function as a versioned remote function.

    **Locally** (at decoration time):
    1. Derives Arrow schemas from the function's type hints
    2. Builds a deploy image with a handler shim
    3. Registers the function version via ExternalFunctionCatalogService

    **Remotely** (inside the container):
    The decorator is a no-op — returns the raw function unchanged.
    """

    def decorator(func: Callable) -> Function | Callable:
        if os.environ.get(_REMOTE_ENV_VAR):
            return func

        f = Function(
            func,
            image=image,
            name=name,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            env=env,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
        )
        f.deploy()
        return f

    return decorator
