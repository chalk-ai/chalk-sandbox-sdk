"""
High-level Sandbox abstraction backed by the Chalk SandboxService.

Usage:
    from managed_sandbox import Sandbox
    from image import Image

    img = Image.debian_slim().pip_install(["requests"]).add_local_file("app.py", "/app/app.py")

    s = Sandbox(image=img).run()
    result = s.exec("cat", "/app/app.py")
    print(result.stdout_text)
    s.terminate()

Lazy local files tracked by the Image are uploaded to a one-off Chalk
volume which is mounted into the sandbox via ChalkFS at each file's
destination path's common ancestor.

Lifecycle RPCs (Create/Get/Terminate, Volume) use Connect protocol (HTTP).
Exec uses gRPC bidirectional streaming via SandboxClient.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import grpc
import httpx

from image import Image, LazyLocalFile
from sandbox import (
    ExecEvent,
    ExecProcess,
    ExecResult,
    SandboxHandle,
    SandboxClient,
    SandboxError as _BaseSandboxError,
    SandboxInfo,
    SandboxNotFoundError,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ManagedSandboxError(_BaseSandboxError):
    """Base exception for managed Sandbox errors."""


class ImageBuildError(ManagedSandboxError):
    """Raised when the sandbox image build fails."""


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------

class Sandbox:
    """A managed sandbox backed by the Chalk SandboxService.

    Orchestrates image building, local file upload via volumes, and
    sandbox lifecycle in a single high-level API — analogous to the
    Container class.

    Uses Connect protocol (HTTP) for lifecycle RPCs and gRPC for exec.
    """

    def __init__(
        self,
        *,
        image: Image | str,
        name: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._image = image
        self._name = name
        self._cpu = cpu
        self._memory = memory
        self._env = env or {}

        # Set after .run()
        self._sandbox_id: str | None = None
        self._sandbox_info: SandboxInfo | None = None
        self._sandbox_handle: SandboxHandle | None = None  # gRPC handle for exec
        self._volumes: list[tuple[str, str]] = []  # [(volume_name, mount_path)]

        # gRPC client for exec (lazily created)
        self._grpc_client: SandboxClient | None = None

        # Auth state (lazily populated)
        self._api_server: str | None = None
        self._access_token: str | None = None
        self._env_id: str | None = None
        self._connect_headers: dict[str, str] | None = None

    # -- Alternate constructors ------------------------------------------------

    @classmethod
    def from_id(cls, sandbox_id: str) -> Sandbox:
        """Attach to an existing sandbox by ID."""
        s = cls._make_empty()
        s._ensure_auth()
        resp = s._sandbox_rpc("GetSandbox", {"sandboxId": sandbox_id})
        if resp.status_code != 200:
            raise ManagedSandboxError(f"GetSandbox failed: {resp.text}")
        data = resp.json()
        s._sandbox_id = sandbox_id
        s._sandbox_info = _parse_sandbox_response(data["sandbox"])
        s._ensure_grpc_handle()
        return s

    @classmethod
    def from_name(cls, name: str) -> Sandbox:
        """Attach to an existing sandbox by name."""
        s = cls._make_empty()
        s._ensure_auth()
        resp = s._sandbox_rpc("ListSandboxes", {})
        if resp.status_code != 200:
            raise ManagedSandboxError(f"ListSandboxes failed: {resp.text}")
        for sb in resp.json().get("sandboxes", []):
            if sb.get("name") == name:
                s._sandbox_id = sb["id"]
                s._sandbox_info = _parse_sandbox_response(sb)
                s._name = name
                s._ensure_grpc_handle()
                return s
        raise SandboxNotFoundError(f"Sandbox with name {name!r} not found")

    @classmethod
    def _make_empty(cls) -> Sandbox:
        s = cls.__new__(cls)
        s._image = None  # type: ignore[assignment]
        s._name = None
        s._cpu = None
        s._memory = None
        s._env = {}
        s._sandbox_id = None
        s._sandbox_info = None
        s._sandbox_handle = None
        s._volumes = []
        s._grpc_client = None
        s._api_server = None
        s._access_token = None
        s._env_id = None
        s._connect_headers = None
        return s

    # -- Auth ------------------------------------------------------------------

    def _ensure_auth(self) -> None:
        if self._access_token is not None:
            return

        self._api_server = os.environ.get("CHALK_API_SERVER", "")
        client_id = os.environ.get("CHALK_CLIENT_ID", "")
        client_secret = os.environ.get("CHALK_CLIENT_SECRET", "")
        self._env_id = os.environ.get("CHALK_ENVIRONMENT_ID", "")

        if not self._api_server or not client_id:
            raise ManagedSandboxError(
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

    def _sandbox_rpc(self, method: str, body: dict, timeout: float = 30) -> httpx.Response:
        return self._connect_rpc(
            "chalk.sandbox.v1.SandboxService", method, body, timeout,
        )

    def _volume_rpc(self, method: str, body: dict, timeout: float = 30) -> httpx.Response:
        return self._connect_rpc(
            "chalk.volume.v1.VolumeService", method, body, timeout,
        )

    # -- gRPC client for exec --------------------------------------------------

    def _ensure_grpc_handle(self) -> None:
        """Create the gRPC SandboxClient and handle for exec."""
        if self._sandbox_handle is not None:
            return

        assert self._api_server is not None
        assert self._access_token is not None
        assert self._sandbox_id is not None

        host = self._api_server.replace("https://", "").replace("http://", "")
        target = f"{host}:443"
        credentials = grpc.ssl_channel_credentials()
        metadata: list[tuple[str, str]] = [
            ("authorization", f"Bearer {self._access_token}"),
            ("x-chalk-env-id", self._env_id or ""),
        ]

        self._grpc_client = SandboxClient(
            target, credentials=credentials, metadata=metadata,
        )
        self._sandbox_handle = self._grpc_client.get(self._sandbox_id)

    # -- Volume upload ---------------------------------------------------------

    def _upload_lazy_files(self) -> list[tuple[str, str]]:
        """Create volumes and upload lazy local files via Connect protocol.

        Groups files by destination directory. Creates one volume per unique
        directory and uploads files with flat names.

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
            vol_name = f"sandbox-files-{uuid.uuid4().hex[:12]}"
            resp = self._volume_rpc("CreateVolume", {"name": vol_name})
            if resp.status_code != 200:
                raise ManagedSandboxError(f"CreateVolume failed: {resp.text}")

            for lf in files:
                content = Path(lf.src).read_bytes()
                encoded = base64.b64encode(content).decode("ascii")
                flat_name = Path(lf.dest).name
                put_resp = self._volume_rpc(
                    "PutFile",
                    {"volumeName": vol_name, "path": flat_name, "data": encoded},
                )
                if put_resp.status_code != 200:
                    raise ManagedSandboxError(
                        f"PutFile failed for {lf.dest}: {put_resp.text}"
                    )

            volumes.append((vol_name, dest_dir))

        return volumes

    # -- Lifecycle -------------------------------------------------------------

    def run(
        self,
        *,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
    ) -> Sandbox:
        """Build the image, upload local files, and start the sandbox.

        Returns self for chaining.
        """
        self._ensure_auth()

        # 1. Upload lazy local files to a volume (if any)
        lazy_files = (
            self._image.lazy_local_files if isinstance(self._image, Image) else set()
        )
        if lazy_files:
            print(f"Uploading {len(lazy_files)} local file(s) to volume(s)...")
            self._volumes = self._upload_lazy_files()
            for vn, mp in self._volumes:
                print(f"  Volume created: {vn} (mount: {mp})")

        # 2. Build the CreateSandbox request body
        sandbox_name = self._name or f"sandbox-{uuid.uuid4().hex[:8]}"
        body: dict = {}

        if isinstance(self._image, Image):
            from google.protobuf.json_format import MessageToDict
            body["imageSpec"] = MessageToDict(self._image.to_proto())
        else:
            body["image"] = self._image

        body["name"] = sandbox_name

        if self._cpu or self._memory:
            resource_limits: dict = {}
            if self._cpu:
                resource_limits["cpu"] = self._cpu
            if self._memory:
                resource_limits["memory"] = self._memory
            body["resourceLimits"] = resource_limits

        if self._env:
            body["env"] = self._env

        if self._volumes:
            body["volumes"] = [
                {"name": vol_name, "mountPath": mount_path, "type": "chalkfs"}
                for vol_name, mount_path in self._volumes
            ]

        # 3. Create the sandbox via Connect protocol
        print(f"Starting sandbox '{sandbox_name}'...")
        resp = self._sandbox_rpc("CreateSandbox", body, timeout=60)
        if resp.status_code != 200:
            raise ManagedSandboxError(f"CreateSandbox failed: {resp.text}")

        data = resp.json()
        self._sandbox_info = _parse_sandbox_response(data["sandbox"])
        self._sandbox_id = self._sandbox_info.id
        print(
            f"Sandbox created: {self._sandbox_id} "
            f"(state={self._sandbox_info.state})"
        )

        # 4. Poll until ready
        if self._sandbox_info.state != "ready":
            self._poll_until_ready(
                poll_interval=poll_interval, timeout=timeout,
            )

        # 5. Set up gRPC handle for exec
        self._ensure_grpc_handle()

        # 6. Wait for ChalkFS volume files to become visible
        if lazy_files:
            self._wait_for_volume_files(timeout=120.0, poll_interval=5.0)

        return self

    def _poll_until_ready(
        self, *, poll_interval: float, timeout: float,
    ) -> None:
        assert self._sandbox_id is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            resp = self._sandbox_rpc(
                "GetSandbox", {"sandboxId": self._sandbox_id},
            )
            if resp.status_code != 200:
                raise ManagedSandboxError(f"GetSandbox failed: {resp.text}")
            info = _parse_sandbox_response(resp.json()["sandbox"])
            self._sandbox_info = info
            print(f"  poll: state={info.state}")
            if info.state == "ready":
                return
            if info.state in ("error", "terminated"):
                raise ManagedSandboxError(
                    f"Sandbox {self._sandbox_id} entered "
                    f"{info.state} state"
                )
        raise ManagedSandboxError(
            f"Sandbox {self._sandbox_id} timed out after {timeout}s"
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

        raise ManagedSandboxError(
            f"ChalkFS files not visible after {timeout}s "
            f"(probed {probe_path})"
        )

    def exec(
        self,
        command: str,
        *args: str,
        timeout_secs: int | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Execute a command and wait for it to complete.

        Delegates to the underlying SandboxHandle's exec method
        (bidirectional gRPC streaming).
        """
        if self._sandbox_handle is None:
            raise ManagedSandboxError("Sandbox is not running. Call .run() first.")

        return self._sandbox_handle.exec(
            command, *args,
            timeout_secs=timeout_secs,
            workdir=workdir,
            env=env,
        )

    def exec_stream(
        self,
        command: str,
        *args: str,
        timeout_secs: int | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Iterator[ExecEvent]:
        """Execute a command and stream events as they arrive."""
        if self._sandbox_handle is None:
            raise ManagedSandboxError("Sandbox is not running. Call .run() first.")

        return self._sandbox_handle.exec_stream(
            command, *args,
            timeout_secs=timeout_secs,
            workdir=workdir,
            env=env,
        )

    def exec_start(
        self,
        command: str,
        *args: str,
        timeout_secs: int | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecProcess:
        """Start a command and return an ExecProcess handle for interactive use."""
        if self._sandbox_handle is None:
            raise ManagedSandboxError("Sandbox is not running. Call .run() first.")

        return self._sandbox_handle.exec_start(
            command, *args,
            timeout_secs=timeout_secs,
            workdir=workdir,
            env=env,
        )

    def terminate(self, grace_period_seconds: int | None = None) -> None:
        """Terminate the sandbox and clean up temporary volumes."""
        self._ensure_auth()

        if self._sandbox_id is not None:
            body: dict = {"sandboxId": self._sandbox_id}
            if grace_period_seconds is not None:
                body["gracePeriodSeconds"] = grace_period_seconds
            self._sandbox_rpc("TerminateSandbox", body)
            self._sandbox_id = None
            self._sandbox_handle = None
            self._sandbox_info = None

        for vol_name, _ in self._volumes:
            self._volume_rpc("DeleteVolume", {"name": vol_name})
        self._volumes = []

    def refresh(self) -> SandboxInfo:
        """Fetch latest sandbox status from the server."""
        if self._sandbox_id is None:
            raise ManagedSandboxError("No sandbox — call .run() first.")
        self._ensure_auth()
        resp = self._sandbox_rpc(
            "GetSandbox", {"sandboxId": self._sandbox_id},
        )
        if resp.status_code != 200:
            raise ManagedSandboxError(f"GetSandbox failed: {resp.text}")
        self._sandbox_info = _parse_sandbox_response(resp.json()["sandbox"])
        return self._sandbox_info

    @property
    def id(self) -> str | None:
        return self._sandbox_id

    @property
    def info(self) -> SandboxInfo | None:
        return self._sandbox_info

    def __repr__(self) -> str:
        state = self._sandbox_info.state if self._sandbox_info else "not started"
        return f"Sandbox(name={self._name!r}, state={state})"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_sandbox_response(data: dict) -> SandboxInfo:
    return SandboxInfo(
        id=data["id"],
        state=data.get("state", ""),
        created_at=data.get("createdAt", ""),
        name=data.get("name"),
        build_id=data.get("buildId"),
    )
