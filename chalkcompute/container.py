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
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chalkcompute._connect import ConnectClient, upload_lazy_files
from chalkcompute._tracing import traced

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from chalkcompute.image import Image as _Image
    from chalkcompute.secret import Secret

_SVC = "chalk.container.v1.ContainerService"
_IMAGE_SVC = "chalk.sandbox.v1.CustomImageService"


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
        image: _Image | str,
        name: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        gpu: str | None = None,
        env: dict[str, str] | None = None,
        entrypoint: list[str] | None = None,
        port: int | None = None,
        lifetime: str | None = None,
        enable_ssh: bool = False,
        volumes: list[tuple[str, str]] | None = None,
        secrets: list[Secret] | None = None,
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
        self._secrets = list(secrets or [])

        # Set after .run()
        self._built_image_uri: str | None = None
        self._container_id: str | None = None
        self._container_info: ContainerInfo | None = None
        self._volumes: list[tuple[str, str]] = list(volumes or [])
        self._upserted_secret_names: list[str] = []

        self._client = ConnectClient()

    # -- Alternate constructors ------------------------------------------------

    @classmethod
    def from_id(cls, container_id: str) -> Container:
        """Attach to an existing container by ID."""
        c = cls._make_empty()
        resp = c._client.rpc(_SVC, "GetContainer", {"id": container_id})
        if resp.status_code != 200:
            raise ContainerError(f"GetContainer failed: {resp.text}")
        c._container_info = _parse_container_response(resp.json()["container"])
        c._container_id = c._container_info.id
        return c

    @classmethod
    def from_name(cls, name: str) -> Container:
        """Attach to an existing container by name."""
        c = cls._make_empty()
        resp = c._client.rpc(_SVC, "GetContainer", {"name": name})
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
        c._secrets = []
        c._built_image_uri = None
        c._container_id = None
        c._container_info = None
        c._volumes = []
        c._upserted_secret_names = []
        c._client = ConnectClient()
        return c

    # -- Image building (Container uses BuildCustomImage with retries) ---------

    def _build_image(
        self, *, poll_interval: float = 0.5, timeout: float = 600.0,
    ) -> str:
        """Build the Image spec via CustomImageService and return the image URI."""
        from chalkcompute._connect import ConnectError, build_image
        from chalkcompute.image import Image

        assert isinstance(self._image, Image)
        try:
            return build_image(
                self._client, self._image,
                poll_interval=poll_interval, timeout=timeout,
            )
        except ConnectError as e:
            raise ImageBuildError(str(e)) from e

    # -- Lifecycle -------------------------------------------------------------

    @traced("chalkcompute.container.run")
    def run(
        self,
        *,
        poll_interval: float = 0.5,
        build_timeout: float = 600.0,
        ready_timeout: float = 300.0,
    ) -> Container:
        """Build the image, upload local files, and start the container.

        Returns self for chaining.
        """
        from chalkcompute._progress import (
            progress_add,
            progress_done,
            progress_fail,
            progress_finish,
        )
        from chalkcompute.image import Image

        container_name = self._name or f"container-{uuid.uuid4().hex[:8]}"
        self._name = container_name
        ckey = f"container:{container_name}"
        progress_add(ckey, "container", f"Container \"{container_name}\"")

        try:
            # 1. Resolve image URI
            if isinstance(self._image, Image):
                ikey = f"image:{container_name}"
                progress_add(ikey, "image", "Building image", parent=ckey)
                logger.info("Building image from %r...", self._image)
                self._built_image_uri = self._build_image(
                    poll_interval=poll_interval, timeout=build_timeout,
                )
                logger.info("Image built: %s", self._built_image_uri)
                progress_done(ikey, detail="cached" if self._built_image_uri else "built")
            else:
                self._built_image_uri = self._image

            # 2. Upload lazy local files to a volume (if any)
            lazy_files = (
                self._image.lazy_local_files if isinstance(self._image, Image) else set()
            )
            if lazy_files:
                vkey = f"volume:{container_name}"
                progress_add(
                    vkey, "volume",
                    f"Uploading {len(lazy_files)} file(s)",
                    parent=ckey,
                )
                logger.info("Uploading %d local file(s) to volume(s)...", len(lazy_files))
                assert isinstance(self._image, Image)
                self._volumes += upload_lazy_files(
                    self._client, self._image, volume_prefix="container-files",
                )
                for vn, mp in self._volumes:
                    logger.info("  Volume created: %s (mount: %s)", vn, mp)
                progress_done(vkey)

            # 3. Resolve lazy secrets (from_local_env / from_local_env_file)
            if any(s.is_lazy for s in self._secrets):
                skey = f"secret:{container_name}"
                progress_add(skey, "secret", "Resolving secrets", parent=ckey)
                from chalkcompute.secret import resolve_lazy_secrets

                resolved, upserted = resolve_lazy_secrets(self._secrets, self._client)
                self._secrets = resolved
                self._upserted_secret_names = upserted
                logger.info("Upserted %d secret(s) from local environment.", len(upserted))
                progress_done(skey, detail=f"{len(upserted)} secret(s)")

            # 4. Build the ContainerService spec
            spec: dict[str, Any] = {
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

            resources: dict[str, str] = {}
            if self._cpu:
                resources["cpu"] = self._cpu
            if self._memory:
                resources["memory"] = self._memory
            if self._gpu:
                resources["gpu"] = self._gpu
            if resources:
                spec["resources"] = resources

            if self._volumes:
                spec["volumes"] = [
                    {"name": vol_name, "mountPath": mount_path, "type": "chalkfs"}
                    for vol_name, mount_path in self._volumes
                ]

            if self._secrets:
                spec["secretRefs"] = [s._to_proto_dict() for s in self._secrets]

            logger.info("Starting container '%s'...", container_name)
            resp = self._client.rpc(
                _SVC, "RunContainer", {"container": {"spec": spec}}, timeout=60,
            )
            if resp.status_code != 200:
                raise ContainerError(f"RunContainer failed: {resp.text}")

            data = resp.json()
            self._container_info = _parse_container_response(data["container"])
            self._container_id = self._container_info.id
            logger.info(
                "Container created: %s (status=%s)",
                self._container_id, self._container_info.status,
            )

            # 5. Poll until Running
            if self._container_info.status != "Running":
                from chalkcompute._progress import progress_update

                progress_update(ckey, detail="booting")
                self._poll_until_running(
                    poll_interval=poll_interval, timeout=ready_timeout,
                )

            # 6. Wait for ChalkFS volume files to become visible
            if lazy_files:
                from chalkcompute._progress import progress_update

                fkey = f"filesync:{container_name}"
                progress_add(fkey, "volume", "Syncing files", parent=ckey)
                self._wait_for_volume_files(timeout=120.0, poll_interval=5.0)
                progress_done(fkey)

            progress_done(ckey, detail="running")
        except BaseException:
            progress_fail(ckey)
            raise
        finally:
            progress_finish()

        return self

    def _poll_until_running(
        self, *, poll_interval: float, timeout: float,
    ) -> None:
        assert self._container_id is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self._client.rpc(_SVC, "GetContainer", {"id": self._container_id})
            if resp.status_code != 200:
                raise ContainerError(f"GetContainer failed: {resp.text}")
            info = _parse_container_response(resp.json()["container"])
            self._container_info = info
            logger.info("  poll: status=%s", info.status)
            if info.status == "Running":
                return
            if info.status in ("Failed", "Succeeded"):
                raise ContainerError(
                    f"Container {self._container_id} entered {info.status} state"
                )
            time.sleep(poll_interval)
        raise ContainerError(
            f"Container {self._container_id} timed out after {timeout}s"
        )

    def _wait_for_volume_files(
        self, *, timeout: float = 120.0, poll_interval: float = 0.5,
    ) -> None:
        """Poll until at least one uploaded file is visible in the ChalkFS mount."""
        from chalkcompute.image import Image

        assert isinstance(self._image, Image)
        sample = next(iter(self._image.lazy_local_files))
        probe_path = sample.dest
        logger.info("Waiting for ChalkFS to sync (probing %s)...", probe_path)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.exec("test", "-f", probe_path)
            if result.exit_code == 0:
                logger.info("ChalkFS files ready.")
                return
            time.sleep(poll_interval)

        raise ContainerError(
            f"ChalkFS files not visible after {timeout}s (probed {probe_path})"
        )

    @traced("chalkcompute.container.exec")
    def exec(
        self,
        *command: str,
        timeout_secs: int | None = None,
        _retries: int = 12,
        _retry_delay: float = 5.0,
    ) -> ExecResult:
        """Execute a command in the running container."""
        if self._container_id is None:
            raise ContainerError("Container is not running. Call .run() first.")

        body: dict[str, Any] = {
            "id": self._container_id,
            "command": list(command),
        }
        if timeout_secs is not None:
            body["timeout"] = f"{timeout_secs}s"

        # TODO(CHA-8630): Remove retry loop once the API reports accurate
        # Running status (container may not be exec-ready immediately).
        last_err: ContainerError | None = None
        for attempt in range(_retries + 1):
            resp = self._client.rpc(_SVC, "ExecCommand", body, timeout=timeout_secs or 30)
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

    @traced("chalkcompute.container.stop")
    def stop(self, grace_period_seconds: int | None = None) -> None:
        """Stop the container and clean up the temporary volume."""
        if self._container_id is not None:
            body: dict[str, Any] = {"id": self._container_id}
            if grace_period_seconds is not None:
                body["gracePeriodSeconds"] = grace_period_seconds
            self._client.rpc(_SVC, "StopContainer", body)
            self._container_id = None
            self._container_info = None

        for vol_name, _ in self._volumes:
            self._client.rpc("chalk.volume.v1.VolumeService", "DeleteVolume", {"name": vol_name})
        self._volumes = []

        if self._upserted_secret_names:
            from chalkcompute.secret import delete_secrets

            delete_secrets(self._client, self._upserted_secret_names)
            self._upserted_secret_names = []

    def refresh(self) -> ContainerInfo:
        """Fetch latest container status from the server."""
        if self._container_id is None:
            raise ContainerError("No container ID — call .run() first.")
        resp = self._client.rpc(_SVC, "GetContainer", {"id": self._container_id})
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
