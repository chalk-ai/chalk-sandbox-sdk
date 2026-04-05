"""
High-level Container abstraction for Chalk sandboxes.

Usage:
    from container import Container
    from image import Image

    img = Image.debian_slim().pip_install(["requests"]).add_local_file("app.py", "/app/app.py")

    c = Container(image=img)
    c.run()                         # builds image, uploads local files, starts sandbox
    result = c.exec("python", "/app/app.py")
    print(result.stdout_text)
    c.terminate()

Lazy local files tracked by the Image are uploaded to a one-off Chalk volume
and copied into the sandbox at their destination paths after startup.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from pathlib import Path

import httpx

from image import Image
from sandbox import ExecResult, Sandbox, SandboxClient


class ContainerError(Exception):
    """Base exception for Container errors."""


class ImageBuildError(ContainerError):
    """Raised when the custom image build fails."""


class Container:
    """A managed sandbox container backed by a declarative Image.

    Orchestrates image building, local file upload via volumes, and sandbox
    lifecycle in a single high-level API.
    """

    def __init__(
        self,
        *,
        image: Image,
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
        self._built_image_uri: str | None = None
        self._sandbox: Sandbox | None = None
        self._sandbox_client: SandboxClient | None = None
        self._volume_name: str | None = None

        # Auth state (lazily populated)
        self._api_server: str | None = None
        self._access_token: str | None = None
        self._env_id: str | None = None
        self._connect_headers: dict[str, str] | None = None
        self._grpc_metadata: list[tuple[str, str]] | None = None

    # -- Alternate constructors ------------------------------------------------

    @classmethod
    def from_id(cls, sandbox_id: str) -> Container:
        """Attach to an existing sandbox by ID.

        The returned container has no image and cannot be re-run, but supports
        exec() and terminate().
        """
        c = cls.__new__(cls)
        c._image = None  # type: ignore[assignment]
        c._name = None
        c._cpu = None
        c._memory = None
        c._env = {}
        c._built_image_uri = None
        c._volume_name = None
        c._api_server = None
        c._access_token = None
        c._env_id = None
        c._connect_headers = None
        c._grpc_metadata = None
        c._sandbox_client = None
        c._sandbox = None

        c._ensure_auth()
        c._ensure_grpc_client()
        assert c._sandbox_client is not None
        c._sandbox = c._sandbox_client.get(id=sandbox_id)
        return c

    @classmethod
    def from_name(cls, name: str) -> Container:
        """Attach to an existing sandbox by name.

        Lists all sandboxes and finds the first one matching the given name.
        """
        c = cls.__new__(cls)
        c._image = None  # type: ignore[assignment]
        c._name = name
        c._cpu = None
        c._memory = None
        c._env = {}
        c._built_image_uri = None
        c._volume_name = None
        c._api_server = None
        c._access_token = None
        c._env_id = None
        c._connect_headers = None
        c._grpc_metadata = None
        c._sandbox_client = None
        c._sandbox = None

        c._ensure_auth()
        c._ensure_grpc_client()
        assert c._sandbox_client is not None
        for info in c._sandbox_client.list():
            if info.name == name and info.state == "ready":
                c._sandbox = c._sandbox_client.get(id=info.id)
                return c
        raise ContainerError(f"No ready sandbox found with name {name!r}")

    # -- Auth & client setup ---------------------------------------------------

    def _ensure_auth(self) -> None:
        """Load credentials from environment and exchange for an access token."""
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
        self._grpc_metadata = [
            ("authorization", f"Bearer {self._access_token}"),
            ("x-chalk-env-id", self._env_id or ""),
        ]

    def _ensure_grpc_client(self) -> None:
        """Create gRPC SandboxClient if not already created."""
        if self._sandbox_client is not None:
            return
        self._ensure_auth()
        import grpc

        assert self._api_server is not None
        host = self._api_server.replace("https://", "").replace("http://", "")
        target = f"{host}:443"
        credentials = grpc.ssl_channel_credentials()
        self._sandbox_client = SandboxClient(
            target, credentials=credentials, metadata=self._grpc_metadata,
        )

    # -- Connect-protocol helpers ----------------------------------------------

    def _connect_rpc(self, service: str, method: str, body: dict, timeout: float = 30) -> httpx.Response:
        assert self._api_server is not None
        assert self._connect_headers is not None
        return httpx.post(
            f"{self._api_server}/{service}/{method}",
            json=body,
            headers=self._connect_headers,
            timeout=timeout,
        )

    def _sandbox_rpc(self, method: str, body: dict, timeout: float = 30) -> httpx.Response:
        return self._connect_rpc("chalk.sandbox.v1.SandboxService", method, body, timeout)

    def _custom_image_rpc(self, method: str, body: dict, timeout: float = 30) -> httpx.Response:
        return self._connect_rpc("chalk.sandbox.v1.CustomImageService", method, body, timeout)

    def _volume_rpc(self, method: str, body: dict, timeout: float = 30) -> httpx.Response:
        return self._connect_rpc("chalk.volume.v1.VolumeService", method, body, timeout)

    # -- Image building --------------------------------------------------------

    def _build_image(self, *, poll_interval: float = 5.0, timeout: float = 600.0) -> str:
        """Build the Image spec via CustomImageService and return the image URI."""
        from google.protobuf.json_format import MessageToDict

        spec_dict = MessageToDict(self._image.to_proto())
        resp = self._custom_image_rpc("BuildCustomImage", {"imageSpec": spec_dict}, timeout=60)
        if resp.status_code != 200:
            raise ImageBuildError(f"BuildCustomImage failed: {resp.text}")

        data = resp.json()
        build_id = data["buildId"]
        image_uri = data.get("image", "")

        # Poll until build succeeds
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            get_resp = self._custom_image_rpc("GetCustomImage", {"buildId": build_id})
            if get_resp.status_code != 200:
                raise ImageBuildError(f"GetCustomImage failed: {get_resp.text}")
            get_data = get_resp.json()
            status = get_data.get("status", "")
            if status == "succeeded":
                return get_data["image"]
            if status == "failed":
                raise ImageBuildError(
                    f"Image build {build_id} failed: {get_data.get('error', 'unknown')}"
                )
            time.sleep(poll_interval)

        raise ImageBuildError(f"Image build {build_id} timed out after {timeout}s")

    # -- Volume upload ---------------------------------------------------------

    def _upload_lazy_files(self) -> str:
        """Create a temporary volume and upload all lazy local files. Returns volume name."""
        vol_name = f"container-files-{uuid.uuid4().hex[:12]}"

        resp = self._volume_rpc("CreateVolume", {"name": vol_name})
        if resp.status_code != 200:
            raise ContainerError(f"CreateVolume failed: {resp.text}")

        for lf in self._image.lazy_local_files:
            content = Path(lf.src).read_bytes()
            encoded = base64.b64encode(content).decode("ascii")
            # Use the dest path (strip leading /) as the volume file path
            vol_path = lf.dest.lstrip("/")
            put_resp = self._volume_rpc(
                "PutFile",
                {"volumeName": vol_name, "path": vol_path, "data": encoded},
            )
            if put_resp.status_code != 200:
                raise ContainerError(f"PutFile failed for {lf.dest}: {put_resp.text}")

        return vol_name

    def _inject_files_via_exec(self) -> None:
        """Copy uploaded volume files into the sandbox at their destination paths."""
        assert self._sandbox is not None
        assert self._volume_name is not None

        for lf in self._image.lazy_local_files:
            vol_path = lf.dest.lstrip("/")
            content = Path(lf.src).read_bytes()

            # Ensure parent directory exists, then write file via base64 decode
            encoded = base64.b64encode(content).decode("ascii")
            dest_dir = str(Path(lf.dest).parent)
            script = f"mkdir -p {dest_dir} && echo '{encoded}' | base64 -d > {lf.dest}"
            if lf.mode is not None:
                script += f" && chmod {oct(lf.mode)[2:]} {lf.dest}"

            result = self._sandbox.exec("sh", "-c", script, timeout_secs=30)
            if result.exit_code != 0:
                raise ContainerError(
                    f"Failed to inject {lf.dest}: exit_code={result.exit_code} "
                    f"stderr={result.stderr_text}"
                )

    # -- Lifecycle -------------------------------------------------------------

    def run(
        self,
        *,
        poll_interval: float = 5.0,
        build_timeout: float = 600.0,
        sandbox_timeout: float = 300.0,
    ) -> Container:
        """Build the image, upload local files, and start the sandbox.

        Returns self for chaining.
        """
        self._ensure_auth()
        self._ensure_grpc_client()

        # 1. Build the image
        print(f"Building image from {self._image!r}...")
        self._built_image_uri = self._build_image(
            poll_interval=poll_interval, timeout=build_timeout,
        )
        print(f"Image built: {self._built_image_uri}")

        # 2. Upload lazy local files to a volume (if any)
        lazy_files = self._image.lazy_local_files
        if lazy_files:
            print(f"Uploading {len(lazy_files)} local file(s) to volume...")
            self._volume_name = self._upload_lazy_files()
            print(f"Volume created: {self._volume_name}")

        # 3. Create the sandbox with the built image URI
        sandbox_name = self._name or f"container-{uuid.uuid4().hex[:8]}"
        body: dict = {"image": self._built_image_uri, "name": sandbox_name}
        if self._env:
            body["env"] = self._env
        if self._cpu or self._memory:
            limits: dict = {}
            if self._cpu:
                limits["cpu"] = self._cpu
            if self._memory:
                limits["memory"] = self._memory
            body["resourceLimits"] = limits

        print(f"Creating sandbox '{sandbox_name}'...")
        resp = self._sandbox_rpc("CreateSandbox", body, timeout=60)
        if resp.status_code != 200:
            raise ContainerError(f"CreateSandbox failed: {resp.text}")

        data = resp.json()
        sandbox_id = data["sandbox"]["id"]
        state = data["sandbox"]["state"]

        # Poll until ready
        if state != "ready":
            deadline = time.monotonic() + sandbox_timeout
            while time.monotonic() < deadline:
                time.sleep(poll_interval)
                get_resp = self._sandbox_rpc("GetSandbox", {"sandboxId": sandbox_id})
                if get_resp.status_code != 200:
                    raise ContainerError(f"GetSandbox failed: {get_resp.text}")
                state = get_resp.json()["sandbox"]["state"]
                if state == "ready":
                    break
                if state in ("error", "terminated"):
                    raise ContainerError(f"Sandbox {sandbox_id} entered {state} state")
            else:
                raise ContainerError(
                    f"Sandbox {sandbox_id} timed out after {sandbox_timeout}s"
                )

        assert self._sandbox_client is not None
        self._sandbox = self._sandbox_client.get(id=sandbox_id)
        print(f"Sandbox ready: {sandbox_id}")

        # 4. Inject lazy local files into the sandbox
        if lazy_files:
            print("Injecting local files into sandbox...")
            self._inject_files_via_exec()
            print("Files injected.")

        return self

    def exec(
        self,
        command: str,
        *args: str,
        timeout_secs: int | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Execute a command in the running sandbox."""
        if self._sandbox is None:
            raise ContainerError("Container is not running. Call .run() first.")
        return self._sandbox.exec(
            command, *args,
            timeout_secs=timeout_secs,
            workdir=workdir,
            env=env,
        )

    def terminate(self, grace_period_seconds: int | None = None) -> None:
        """Terminate the sandbox and clean up the temporary volume."""
        if self._sandbox is not None:
            self._sandbox.terminate(grace_period_seconds=grace_period_seconds)
            self._sandbox = None

        if self._volume_name is not None:
            self._volume_rpc("DeleteVolume", {"name": self._volume_name})
            self._volume_name = None

        if self._sandbox_client is not None:
            self._sandbox_client.close()
            self._sandbox_client = None

    @property
    def sandbox_id(self) -> str | None:
        """The underlying sandbox ID, if running."""
        return self._sandbox.id if self._sandbox else None

    @property
    def image_uri(self) -> str | None:
        """The built image URI, if available."""
        return self._built_image_uri

    def __repr__(self) -> str:
        state = "running" if self._sandbox else "stopped"
        return f"Container(name={self._name!r}, state={state})"
