"""
High-level ScalingGroup abstraction backed by the Chalk ScalingGroupManagerService.

Usage:
    from pathlib import Path
    from chalkcompute import ScalingGroup, Image

    Path("app.py").write_text(
        "from flask import Flask\n\n"
        "app = Flask(__name__)\n\n"
        "@app.get('/health')\n"
        "def health():\n"
        "    return {'ok': True}\n\n"
        "app.run(host='0.0.0.0', port=8080)\n"
    )

    img = (
        Image.debian_slim()
        .pip_install(["flask"])
        .add_local_file("./app.py", "/app/app.py")
        .entrypoint(["python", "/app/app.py"])
    )

    sg = ScalingGroup(image=img, port=8080).deploy().wait_ready()
    resp = sg.call("/health", method="GET")
    print(resp.status_code)
    sg.delete()
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chalkcompute._connect import ConnectClient, build_image, upload_lazy_files

if TYPE_CHECKING:
    import httpx

    from chalkcompute.image import Image as _Image

_SVC = "chalk.scalinggroup.v1.ScalingGroupManagerService"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ScalingGroupError(Exception):
    """Base exception for ScalingGroup errors."""


class ScalingGroupBuildError(ScalingGroupError):
    """Raised when the custom image build fails."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ScalingGroupInfo:
    """Metadata about a scaling group."""

    id: str
    name: str
    status: str
    web_url: str | None = None
    ready_replicas: int = 0
    available_replicas: int = 0


# ---------------------------------------------------------------------------
# ScalingGroup
# ---------------------------------------------------------------------------


class ScalingGroup:
    """A managed scaling group backed by the Chalk ScalingGroupManagerService.

    Orchestrates image building, local file upload via volumes, and
    scaling group lifecycle in a single high-level API.
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
        port: int = 8080,
        protocol: str = "http",
        min_replicas: int = 1,
        max_replicas: int = 1,
    ) -> None:
        self._image = image
        self._name = name
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        self._env = env or {}
        self._entrypoint = entrypoint
        self._port = port
        self._protocol = protocol
        self._min_replicas = min_replicas
        self._max_replicas = max_replicas

        # Set after .deploy()
        self._built_image_uri: str | None = None
        self._scaling_group_id: str | None = None
        self._scaling_group_info: ScalingGroupInfo | None = None
        self._volumes: list[tuple[str, str]] = []

        self._client = ConnectClient()

    # -- Alternate constructors ------------------------------------------------

    @classmethod
    def from_id(cls, scaling_group_id: str) -> ScalingGroup:
        """Attach to an existing scaling group by ID."""
        sg = cls._make_empty()
        resp = sg._client.rpc(_SVC, "GetScalingGroup", {"id": scaling_group_id})
        if resp.status_code != 200:
            raise ScalingGroupError(f"GetScalingGroup failed: {resp.text}")
        sg._scaling_group_info = _parse_scaling_group_response(resp.json()["scalingGroup"])
        sg._scaling_group_id = sg._scaling_group_info.id
        return sg

    @classmethod
    def from_name(cls, name: str) -> ScalingGroup:
        """Attach to an existing scaling group by name."""
        sg = cls._make_empty()
        resp = sg._client.rpc(_SVC, "GetScalingGroup", {"name": name})
        if resp.status_code != 200:
            raise ScalingGroupError(f"GetScalingGroup failed: {resp.text}")
        sg._scaling_group_info = _parse_scaling_group_response(resp.json()["scalingGroup"])
        sg._scaling_group_id = sg._scaling_group_info.id
        sg._name = name
        return sg

    @classmethod
    def _make_empty(cls) -> ScalingGroup:
        sg = cls.__new__(cls)
        sg._image = None  # type: ignore[assignment]
        sg._name = None
        sg._cpu = None
        sg._memory = None
        sg._gpu = None
        sg._env = {}
        sg._entrypoint = None
        sg._port = 8080
        sg._protocol = "http"
        sg._min_replicas = 1
        sg._max_replicas = 1
        sg._built_image_uri = None
        sg._scaling_group_id = None
        sg._scaling_group_info = None
        sg._volumes = []
        sg._client = ConnectClient()
        return sg

    # -- Lifecycle -------------------------------------------------------------

    def deploy(
        self,
        *,
        poll_interval: float = 5.0,
        build_timeout: float = 600.0,
        ready_timeout: float = 300.0,
    ) -> ScalingGroup:
        """Build the image, upload local files, and create the scaling group.

        Returns self for chaining.
        """
        from chalkcompute.image import Image

        # 1. Resolve image URI
        if isinstance(self._image, Image):
            print(f"Building image from {self._image!r}...")
            self._built_image_uri = build_image(
                self._client, self._image,
                poll_interval=poll_interval, timeout=build_timeout,
            )
            print(f"Image built: {self._built_image_uri}")
        else:
            self._built_image_uri = self._image

        # 2. Upload lazy local files to volumes (if any)
        lazy_files = (
            self._image.lazy_local_files if isinstance(self._image, Image) else set()
        )
        if lazy_files:
            print(f"Uploading {len(lazy_files)} local file(s) to volume(s)...")
            assert isinstance(self._image, Image)
            self._volumes = upload_lazy_files(
                self._client, self._image, volume_prefix="sg-files",
            )
            for vn, mp in self._volumes:
                print(f"  Volume created: {vn} (mount: {mp})")

        # 3. Build the ScalingGroupSpec
        sg_name = self._name or f"sg-{uuid.uuid4().hex[:8]}"
        container_spec: dict[str, Any] = {
            "name": sg_name,
            "image": self._built_image_uri,
            "port": self._port,
            "protocol": self._protocol,
        }

        if self._entrypoint:
            container_spec["entrypoint"] = self._entrypoint
        if self._env:
            container_spec["envVars"] = self._env

        resources: dict[str, str] = {}
        if self._cpu:
            resources["cpu"] = self._cpu
        if self._memory:
            resources["memory"] = self._memory
        if self._gpu:
            resources["gpu"] = self._gpu
        if resources:
            container_spec["resources"] = resources

        if self._volumes:
            container_spec["volumes"] = [
                {"name": vol_name, "mountPath": mount_path, "type": "chalkfs"}
                for vol_name, mount_path in self._volumes
            ]

        spec: dict[str, Any] = {
            "containerSpec": container_spec,
            "scalingSpec": {
                "minReplicas": self._min_replicas,
                "maxReplicas": self._max_replicas,
            },
        }

        print(f"Deploying scaling group '{sg_name}'...")
        resp = self._client.rpc(_SVC, "CreateScalingGroup", {"spec": spec}, timeout=60)
        if resp.status_code != 200:
            raise ScalingGroupError(f"CreateScalingGroup failed: {resp.text}")

        data = resp.json()
        self._scaling_group_info = _parse_scaling_group_response(data["scalingGroup"])
        self._scaling_group_id = self._scaling_group_info.id
        print(
            f"Scaling group created: {self._scaling_group_id} "
            f"(status={self._scaling_group_info.status})"
        )

        # 4. Poll until Running
        if self._scaling_group_info.status != "Running":
            self._poll_until_running(
                poll_interval=poll_interval, timeout=ready_timeout,
            )

        return self

    def _poll_until_running(
        self, *, poll_interval: float, timeout: float,
    ) -> None:
        assert self._scaling_group_id is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            resp = self._client.rpc(_SVC, "GetScalingGroup", {"id": self._scaling_group_id})
            if resp.status_code != 200:
                raise ScalingGroupError(f"GetScalingGroup failed: {resp.text}")
            info = _parse_scaling_group_response(resp.json()["scalingGroup"])
            self._scaling_group_info = info
            print(f"  poll: status={info.status}")
            if info.status in ("Running", "Available"):
                return
            if info.status in ("Failed", "Error"):
                raise ScalingGroupError(
                    f"Scaling group {self._scaling_group_id} entered {info.status} state"
                )
        raise ScalingGroupError(
            f"Scaling group {self._scaling_group_id} timed out after {timeout}s"
        )

    def call(
        self,
        path: str = "/",
        *,
        method: str = "POST",
        json: dict | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> httpx.Response:
        """Make an HTTP request to the scaling group's endpoint."""
        import httpx

        if self._scaling_group_info is None or self._scaling_group_info.web_url is None:
            raise ScalingGroupError(
                "Scaling group has no web_url. Call .deploy() first or .refresh() to update."
            )
        url = f"{self._scaling_group_info.web_url}{path}"
        return httpx.request(method, url, json=json, content=data, headers=headers, timeout=timeout)

    def wait_ready(
        self,
        *,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
    ) -> ScalingGroup:
        """Wait until a running scaling group has at least one ready replica.

        Returns self for chaining.
        """
        if self._scaling_group_id is None:
            raise ScalingGroupError("No scaling group ID — call .deploy() first.")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = self.refresh()
            if info.status in ("Failed", "Error"):
                raise ScalingGroupError(
                    f"Scaling group {self._scaling_group_id} entered {info.status} state"
                )
            if (
                info.status in ("Running", "Available")
                and info.ready_replicas > 0
                and info.available_replicas > 0
            ):
                return self
            time.sleep(poll_interval)

        raise ScalingGroupError(
            f"Scaling group {self._scaling_group_id} not ready after {timeout}s "
            f"(status={self._scaling_group_info.status if self._scaling_group_info else 'unknown'}, "
            f"ready={self._scaling_group_info.ready_replicas if self._scaling_group_info else 0}, "
            f"available={self._scaling_group_info.available_replicas if self._scaling_group_info else 0})"
        )

    def delete(self) -> None:
        """Delete the scaling group and clean up volumes."""
        if self._scaling_group_id is not None:
            self._client.rpc(_SVC, "DeleteScalingGroup", {"id": self._scaling_group_id})
            self._scaling_group_id = None
            self._scaling_group_info = None

        for vol_name, _ in self._volumes:
            self._client.rpc("chalk.volume.v1.VolumeService", "DeleteVolume", {"name": vol_name})
        self._volumes = []

    def refresh(self) -> ScalingGroupInfo:
        """Fetch latest scaling group status from the server."""
        if self._scaling_group_id is None:
            raise ScalingGroupError("No scaling group ID — call .deploy() first.")
        resp = self._client.rpc(_SVC, "GetScalingGroup", {"id": self._scaling_group_id})
        if resp.status_code != 200:
            raise ScalingGroupError(f"GetScalingGroup failed: {resp.text}")
        self._scaling_group_info = _parse_scaling_group_response(resp.json()["scalingGroup"])
        return self._scaling_group_info

    @property
    def id(self) -> str | None:
        return self._scaling_group_id

    @property
    def info(self) -> ScalingGroupInfo | None:
        return self._scaling_group_info

    @property
    def image_uri(self) -> str | None:
        return self._built_image_uri

    @property
    def web_url(self) -> str | None:
        if self._scaling_group_info is None:
            return None
        return self._scaling_group_info.web_url

    def __repr__(self) -> str:
        status = (
            self._scaling_group_info.status if self._scaling_group_info else "not deployed"
        )
        return f"ScalingGroup(name={self._name!r}, status={status})"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_scaling_group_response(data: dict) -> ScalingGroupInfo:
    return ScalingGroupInfo(
        id=data["id"],
        name=data["name"],
        status=data["status"],
        web_url=data.get("webUrl"),
        ready_replicas=data.get("readyReplicas", 0),
        available_replicas=data.get("availableReplicas", 0),
    )
