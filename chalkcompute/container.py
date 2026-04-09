"""
High-level Container abstraction backed by the Chalk ContainerService.

Usage:
    from chalkcompute import Container, Image

    img = Image.debian_slim().pip_install(["requests"]).add_local_file("app.py", "/app/app.py")

    c = Container(image=img).run()
    result = c.exec("cat", "/app/app.py")
    print(result.stdout_text)
    c.stop()

Lazy local files tracked by the Image are uploaded to a one-off Chalk
volume which is mounted into the container via ChalkFS at each file's
destination path's common ancestor.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from chalkcompute.image import Image, LazyLocalFile


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ContainerError(Exception):
    """Base exception for Container errors."""


class ImageBuildError(ContainerError):
    """Raised when the custom image build fails."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ExecResult:
    """Result of an ExecCommand call."""
    stdout: bytes
    stderr: bytes
    exit_code: int

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


@dataclass
class ContainerInfo:
    """Metadata about a running container."""
    id: str
    name: str
    status: str
    pod_name: str
    web_url: str | None = None


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------

class Container:
    """A managed container backed by the Chalk ContainerService.

    Orchestrates image building, local file upload via volumes, and
    container lifecycle in a single high-level API.
    """

    def __init__(
        self,
        *,
        image: Image | str,
        name: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        gpu: str | None = None,
        env: dict[str, str] | None = None,
        entrypoint: list[str] | None = None,
        port: int | None = None,
        lifetime: str | None = None,
        enable_ssh: bool = False,
    ) -> None:
        self._image = image
        self._name = name
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        self._env = env or {}
        self._entrypoint = entrypoint
        self._port = port
        self._lifetime = lifetime
        self._enable_ssh = enable_ssh

        # Set after .run()
        self._built_image_uri: str | None = None
        self._container_id: str | None = None
        self._container_info: ContainerInfo | None = None
        self._volumes: list[tuple[str, str]] = []  # [(volume_name, mount_path)]

        # Auth state (lazily populated)
        self._api_server: str | None = None
        self._access_token: str | None = None
        self._env_id: str | None = None
        self._connect_headers: dict[str, str] | None = None

    # -- Alternate constructors ------------------------------------------------

    @classmethod
    def from_id(cls, container_id: str) -> Container:
        """Attach to an existing container by ID."""
        c = cls._make_empty()
        c._ensure_auth()
        resp = c._container_rpc("GetContainer", {"id": container_id})
        if resp.status_code != 200:
            raise ContainerError(f"GetContainer failed: {resp.text}")
        c._container_info = _parse_container_response(resp.json()["container"])
        c._container_id = c._container_info.id
        return c

    @classmethod
    def from_name(cls, name: str) -> Container:
        """Attach to an existing container by name."""
        c = cls._make_empty()
        c._ensure_auth()
        resp = c._container_rpc("GetContainer", {"name": name})
        if resp.status_code != 200:
            raise ContainerError(f"GetContainer failed: {resp.text}")
        c._container_info = _parse_container_response(resp.json()["container"])
        c._container_id = c._container_info.id
        c._name = name
        return c

    @classmethod
    def _make_empty(cls) -> Container:
        c = cls.__new__(cls)
        c._image = None  # type: ignore[assignment]
        c._name = None
        c._cpu = None
        c._memory = None
        c._gpu = None
        c._env = {}
        c._entrypoint = None
        c._port = None
        c._lifetime = None
        c._enable_ssh = False
        c._built_image_uri = None
        c._container_id = None
        c._container_info = None
        c._volumes = []
        c._api_server = None
        c._access_token = None
        c._env_id = None
        c._connect_headers = None
        return c

    # -- Auth ------------------------------------------------------------------

    def _ensure_auth(self) -> None:
        if self._access_token is not None:
            return

        self._api_server = os.environ.get("CHALK_API_SERVER", "")
        client_id = os.environ.get("CHALK_CLIENT_ID", "")
        client_secret = os.environ.get("CHALK_CLIENT_SECRET", "")
        self._env_id = os.environ.get("CHALK_ENVIRONMENT_ID", "")

        if not self._api_server or not client_id:
            raise ContainerError(
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
        assert self._api_server is not None
        assert self._connect_headers is not None
        return httpx.post(
            f"{self._api_server}/{service}/{method}",
            json=body,
            headers=self._connect_headers,
            timeout=timeout,
        )

    def _container_rpc(self, method: str, body: dict, timeout: float = 30) -> httpx.Response:
        return self._connect_rpc(
            "chalk.container.v1.ContainerService", method, body, timeout,
        )

    def _custom_image_rpc(self, method: str, body: dict, timeout: float = 30) -> httpx.Response:
        return self._connect_rpc(
            "chalk.sandbox.v1.CustomImageService", method, body, timeout,
        )

    def _volume_rpc(self, method: str, body: dict, timeout: float = 30) -> httpx.Response:
        return self._connect_rpc(
            "chalk.volume.v1.VolumeService", method, body, timeout,
        )

    # -- Image building --------------------------------------------------------

    def _build_image(
        self, *, poll_interval: float = 5.0, timeout: float = 600.0,
    ) -> str:
        """Build the Image spec via CustomImageService and return the image URI."""
        from google.protobuf.json_format import MessageToDict

        assert isinstance(self._image, Image)
        spec_dict = MessageToDict(self._image.to_proto())
        resp = self._custom_image_rpc(
            "GetOrBuildCustomImage", {"imageSpec": spec_dict},
        )
        if resp.status_code != 200:
            raise ImageBuildError(f"GetOrBuildCustomImage failed: {resp.text}")

        data = resp.json()
        status = data.get("status", "")
        if status == "exists" or status == "succeeded":
            return data["image"]
        if status == "failed":
            raise ImageBuildError(
                f"Image build failed: {data.get('error', 'unknown')}"
            )

        build_id = data["buildId"]

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            get_resp = self._custom_image_rpc(
                "GetCustomImage", {"buildId": build_id},
            )
            if get_resp.status_code != 200:
                raise ImageBuildError(f"GetCustomImage failed: {get_resp.text}")
            get_data = get_resp.json()
            status = get_data.get("status", "")
            if status == "succeeded":
                return get_data["image"]
            if status == "failed":
                raise ImageBuildError(
                    f"Image build {build_id} failed: "
                    f"{get_data.get('error', get_data.get('errorMessage', repr(get_data)))}"
                )
            time.sleep(poll_interval)

        raise ImageBuildError(f"Image build {build_id} timed out after {timeout}s")

    # -- Volume upload ---------------------------------------------------------

    def _upload_lazy_files(self) -> list[tuple[str, str]]:
        """Create volumes and upload lazy local files.

        Groups files by destination directory. Creates one volume per unique
        directory and uploads files with flat names (ChalkFS doesn't support
        nested directory paths in a single mount).

        Returns a list of (volume_name, mount_path) tuples.
        """
        assert isinstance(self._image, Image)

        # Group files by their destination directory
        dir_to_files: dict[str, list[LazyLocalFile]] = {}
        for lf in self._image.lazy_local_files:
            dest_dir = str(Path(lf.dest).parent)
            dir_to_files.setdefault(dest_dir, []).append(lf)

        volumes: list[tuple[str, str]] = []
        for dest_dir, files in dir_to_files.items():
            vol_name = f"container-files-{uuid.uuid4().hex[:12]}"
            resp = self._volume_rpc("CreateVolume", {"name": vol_name})
            if resp.status_code != 200:
                raise ContainerError(f"CreateVolume failed: {resp.text}")

            for lf in files:
                content = Path(lf.src).read_bytes()
                encoded = base64.b64encode(content).decode("ascii")
                flat_name = Path(lf.dest).name
                put_resp = self._volume_rpc(
                    "PutFile",
                    {"volumeName": vol_name, "path": flat_name, "data": encoded},
                )
                if put_resp.status_code != 200:
                    raise ContainerError(
                        f"PutFile failed for {lf.dest}: {put_resp.text}"
                    )

            volumes.append((vol_name, dest_dir))

        return volumes

    # -- Lifecycle -------------------------------------------------------------

    def run(
        self,
        *,
        poll_interval: float = 5.0,
        build_timeout: float = 600.0,
        ready_timeout: float = 300.0,
    ) -> Container:
        """Build the image, upload local files, and start the container.

        Returns self for chaining.
        """
        self._ensure_auth()

        # 1. Resolve image URI
        if isinstance(self._image, Image):
            print(f"Building image from {self._image!r}...")
            self._built_image_uri = self._build_image(
                poll_interval=poll_interval, timeout=build_timeout,
            )
            print(f"Image built: {self._built_image_uri}")
        else:
            self._built_image_uri = self._image

        # 2. Upload lazy local files to a volume (if any)
        lazy_files = (
            self._image.lazy_local_files if isinstance(self._image, Image) else set()
        )
        if lazy_files:
            print(f"Uploading {len(lazy_files)} local file(s) to volume(s)...")
            self._volumes = self._upload_lazy_files()
            for vn, mp in self._volumes:
                print(f"  Volume created: {vn} (mount: {mp})")

        # 3. Build the ContainerService spec
        container_name = self._name or f"container-{uuid.uuid4().hex[:8]}"
        spec: dict = {
            "name": container_name,
            "image": self._built_image_uri,
            "entrypoint": self._entrypoint or ["sleep", "infinity"],
        }

        if self._env:
            spec["envVars"] = self._env
        if self._port is not None:
            spec["port"] = self._port
        if self._lifetime is not None:
            spec["lifetime"] = self._lifetime
        if self._enable_ssh:
            spec["enableSsh"] = True

        resources: dict = {}
        if self._cpu:
            resources["cpu"] = self._cpu
        if self._memory:
            resources["memory"] = self._memory
        if self._gpu:
            resources["gpu"] = self._gpu
        if resources:
            spec["resources"] = resources

        # Mount each volume via ChalkFS at its destination directory
        if self._volumes:
            spec["volumes"] = [
                {
                    "name": vol_name,
                    "mountPath": mount_path,
                    "type": "chalkfs",
                }
                for vol_name, mount_path in self._volumes
            ]

        print(f"Starting container '{container_name}'...")
        resp = self._container_rpc(
            "RunContainer", {"container": {"spec": spec}}, timeout=60,
        )
        if resp.status_code != 200:
            raise ContainerError(f"RunContainer failed: {resp.text}")

        data = resp.json()
        self._container_info = _parse_container_response(data["container"])
        self._container_id = self._container_info.id
        print(
            f"Container created: {self._container_id} "
            f"(status={self._container_info.status})"
        )

        # 4. Poll until Running
        if self._container_info.status != "Running":
            self._poll_until_running(
                poll_interval=poll_interval, timeout=ready_timeout,
            )

        # 5. Wait for ChalkFS volume files to become visible
        if lazy_files:
            self._wait_for_volume_files(timeout=120.0, poll_interval=5.0)

        return self

    def _poll_until_running(
        self, *, poll_interval: float, timeout: float,
    ) -> None:
        assert self._container_id is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            resp = self._container_rpc(
                "GetContainer", {"id": self._container_id},
            )
            if resp.status_code != 200:
                raise ContainerError(f"GetContainer failed: {resp.text}")
            info = _parse_container_response(resp.json()["container"])
            self._container_info = info
            print(f"  poll: status={info.status}")
            if info.status == "Running":
                return
            if info.status in ("Failed", "Succeeded"):
                raise ContainerError(
                    f"Container {self._container_id} entered "
                    f"{info.status} state"
                )
        raise ContainerError(
            f"Container {self._container_id} timed out after {timeout}s"
        )

    def _wait_for_volume_files(
        self, *, timeout: float = 120.0, poll_interval: float = 5.0,
    ) -> None:
        """Poll until at least one uploaded file is visible in the ChalkFS mount."""
        assert isinstance(self._image, Image)
        sample = next(iter(self._image.lazy_local_files))
        probe_path = sample.dest
        print(f"Waiting for ChalkFS to sync (probing {probe_path})...")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.exec("test", "-f", probe_path)
            if result.exit_code == 0:
                print("ChalkFS files ready.")
                return
            time.sleep(poll_interval)

        raise ContainerError(
            f"ChalkFS files not visible after {timeout}s "
            f"(probed {probe_path})"
        )

    def exec(
        self,
        *command: str,
        timeout_secs: int | None = None,
        _retries: int = 12,
        _retry_delay: float = 5.0,
    ) -> ExecResult:
        """Execute a command in the running container via ContainerService.ExecCommand."""
        if self._container_id is None:
            raise ContainerError("Container is not running. Call .run() first.")

        body: dict = {
            "id": self._container_id,
            "command": list(command),
        }
        if timeout_secs is not None:
            body["timeout"] = f"{timeout_secs}s"

        # TODO(CHA-8630): Remove retry loop once the API reports accurate
        # Running status (container may not be exec-ready immediately).
        # Blocked on CHA-8629.
        last_err: ContainerError | None = None
        for attempt in range(_retries + 1):
            resp = self._container_rpc("ExecCommand", body, timeout=timeout_secs or 30)
            if resp.status_code == 200:
                data = resp.json()
                stdout_b64 = data.get("stdout", "")
                stderr_b64 = data.get("stderr", "")
                return ExecResult(
                    stdout=base64.b64decode(stdout_b64) if stdout_b64 else b"",
                    stderr=base64.b64decode(stderr_b64) if stderr_b64 else b"",
                    exit_code=data.get("exitCode", 0),
                )
            last_err = ContainerError(f"ExecCommand failed: {resp.text}")
            if "container not found" in resp.text and attempt < _retries:
                time.sleep(_retry_delay)
                continue
            raise last_err
        assert last_err is not None
        raise last_err

    def stop(self, grace_period_seconds: int | None = None) -> None:
        """Stop the container and clean up the temporary volume."""
        self._ensure_auth()
        if self._container_id is not None:
            body: dict = {"id": self._container_id}
            if grace_period_seconds is not None:
                body["gracePeriodSeconds"] = grace_period_seconds
            self._container_rpc("StopContainer", body)
            self._container_id = None
            self._container_info = None

        for vol_name, _ in self._volumes:
            self._volume_rpc("DeleteVolume", {"name": vol_name})
        self._volumes = []

    def refresh(self) -> ContainerInfo:
        """Fetch latest container status from the server."""
        if self._container_id is None:
            raise ContainerError("No container ID — call .run() first.")
        self._ensure_auth()
        resp = self._container_rpc("GetContainer", {"id": self._container_id})
        if resp.status_code != 200:
            raise ContainerError(f"GetContainer failed: {resp.text}")
        self._container_info = _parse_container_response(resp.json()["container"])
        return self._container_info

    @property
    def id(self) -> str | None:
        return self._container_id

    @property
    def info(self) -> ContainerInfo | None:
        return self._container_info

    @property
    def image_uri(self) -> str | None:
        return self._built_image_uri

    def __repr__(self) -> str:
        status = self._container_info.status if self._container_info else "not started"
        return f"Container(name={self._name!r}, status={status})"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_container_response(data: dict) -> ContainerInfo:
    return ContainerInfo(
        id=data["id"],
        name=data["name"],
        status=data["status"],
        pod_name=data.get("podName", ""),
        web_url=data.get("webUrl"),
    )
