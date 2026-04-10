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

    sg = ScalingGroup(image=img, port=8080).deploy()
    resp = sg.call("/health", method="GET")
    print(resp.status_code)
    sg.delete()
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from chalkcompute.image import Image


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
        image: Image | str,
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
        self._volumes: list[tuple[str, str]] = []  # [(volume_name, mount_path)]

        # Auth state (lazily populated)
        self._api_server: str | None = None
        self._access_token: str | None = None
        self._env_id: str | None = None
        self._connect_headers: dict[str, str] | None = None

    # -- Alternate constructors ------------------------------------------------

    @classmethod
    def from_id(cls, scaling_group_id: str) -> ScalingGroup:
        """Attach to an existing scaling group by ID."""
        sg = cls._make_empty()
        sg._ensure_auth()
        resp = sg._scaling_group_rpc("GetScalingGroup", {"id": scaling_group_id})
        if resp.status_code != 200:
            raise ScalingGroupError(f"GetScalingGroup failed: {resp.text}")
        sg._scaling_group_info = _parse_scaling_group_response(
            resp.json()["scalingGroup"]
        )
        sg._scaling_group_id = sg._scaling_group_info.id
        return sg

    @classmethod
    def from_name(cls, name: str) -> ScalingGroup:
        """Attach to an existing scaling group by name."""
        sg = cls._make_empty()
        sg._ensure_auth()
        resp = sg._scaling_group_rpc("GetScalingGroup", {"name": name})
        if resp.status_code != 200:
            raise ScalingGroupError(f"GetScalingGroup failed: {resp.text}")
        sg._scaling_group_info = _parse_scaling_group_response(
            resp.json()["scalingGroup"]
        )
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
        sg._api_server = None
        sg._access_token = None
        sg._env_id = None
        sg._connect_headers = None
        return sg

    # -- Auth ------------------------------------------------------------------

    def _ensure_auth(self) -> None:
        if self._access_token is not None:
            return

        self._api_server = os.environ.get("CHALK_API_SERVER", "")
        client_id = os.environ.get("CHALK_CLIENT_ID", "")
        client_secret = os.environ.get("CHALK_CLIENT_SECRET", "")
        self._env_id = os.environ.get("CHALK_ENVIRONMENT_ID", "")

        if not self._api_server or not client_id:
            raise ScalingGroupError(
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
        self,
        service: str,
        method: str,
        body: dict,
        timeout: float = 30,
    ) -> httpx.Response:
        assert self._api_server is not None
        assert self._connect_headers is not None
        return httpx.post(
            f"{self._api_server}/{service}/{method}",
            json=body,
            headers=self._connect_headers,
            timeout=timeout,
        )

    def _scaling_group_rpc(
        self, method: str, body: dict, timeout: float = 30
    ) -> httpx.Response:
        return self._connect_rpc(
            "chalk.scalinggroup.v1.ScalingGroupManagerService",
            method,
            body,
            timeout,
        )

    def _custom_image_rpc(
        self, method: str, body: dict, timeout: float = 30
    ) -> httpx.Response:
        return self._connect_rpc(
            "chalk.sandbox.v1.CustomImageService",
            method,
            body,
            timeout,
        )

    def _volume_rpc(
        self, method: str, body: dict, timeout: float = 30
    ) -> httpx.Response:
        return self._connect_rpc(
            "chalk.volume.v1.VolumeService",
            method,
            body,
            timeout,
        )

    # -- Image building --------------------------------------------------------

    def _build_image(
        self,
        *,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
    ) -> str:
        """Build the Image spec via CustomImageService and return the image URI."""
        from google.protobuf.json_format import MessageToDict

        assert isinstance(self._image, Image)
        spec_dict = MessageToDict(self._image.to_proto())
        resp = self._custom_image_rpc(
            "GetOrBuildCustomImage",
            {"imageSpec": spec_dict},
        )
        if resp.status_code != 200:
            raise ScalingGroupBuildError(f"GetOrBuildCustomImage failed: {resp.text}")

        data = resp.json()
        status = data.get("status", "")
        if status == "exists" or status == "succeeded":
            return data["image"]
        if status == "failed":
            raise ScalingGroupBuildError(
                f"Image build failed: {data.get('error', 'unknown')}"
            )

        build_id = data["buildId"]

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            get_resp = self._custom_image_rpc(
                "GetCustomImage",
                {"buildId": build_id},
            )
            if get_resp.status_code != 200:
                raise ScalingGroupBuildError(f"GetCustomImage failed: {get_resp.text}")
            get_data = get_resp.json()
            status = get_data.get("status", "")
            if status == "succeeded":
                return get_data["image"]
            if status == "failed":
                raise ScalingGroupBuildError(
                    f"Image build {build_id} failed: {get_data.get('error', 'unknown')}"
                )
            time.sleep(poll_interval)

        raise ScalingGroupBuildError(
            f"Image build {build_id} timed out after {timeout}s"
        )

    # -- Volume upload ---------------------------------------------------------

    def _upload_lazy_files(self) -> list[tuple[str, str]]:
        """Create volumes and upload lazy local files.

        Groups files by destination directory. Creates one volume per unique
        directory and uploads files with flat names.

        Returns a list of (volume_name, mount_path) tuples.
        """
        assert isinstance(self._image, Image)
        try:
            dir_to_files = self._image.lazy_local_files_grouped_for_volume_mounts()
        except ValueError as e:
            raise ScalingGroupError(str(e)) from e

        volumes: list[tuple[str, str]] = []
        for dest_dir, files in dir_to_files.items():
            vol_name = f"sg-files-{uuid.uuid4().hex[:12]}"
            resp = self._volume_rpc("CreateVolume", {"name": vol_name})
            if resp.status_code != 200:
                raise ScalingGroupError(f"CreateVolume failed: {resp.text}")

            for lf in files:
                content = Path(lf.src).read_bytes()
                encoded = base64.b64encode(content).decode("ascii")
                flat_name = Path(lf.dest).name
                put_resp = self._volume_rpc(
                    "PutFile",
                    {"volumeName": vol_name, "path": flat_name, "data": encoded},
                )
                if put_resp.status_code != 200:
                    raise ScalingGroupError(
                        f"PutFile failed for {lf.dest}: {put_resp.text}"
                    )

            volumes.append((vol_name, dest_dir))

        return volumes

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
        self._ensure_auth()

        # 1. Resolve image URI
        if isinstance(self._image, Image):
            print(f"Building image from {self._image!r}...")
            self._built_image_uri = self._build_image(
                poll_interval=poll_interval,
                timeout=build_timeout,
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
            self._volumes = self._upload_lazy_files()
            for vn, mp in self._volumes:
                print(f"  Volume created: {vn} (mount: {mp})")

        # 3. Build the ScalingGroupSpec
        sg_name = self._name or f"sg-{uuid.uuid4().hex[:8]}"
        container_spec: dict = {
            "name": sg_name,
            "image": self._built_image_uri,
            "port": self._port,
            "protocol": self._protocol,
        }

        if self._entrypoint:
            container_spec["entrypoint"] = self._entrypoint
        if self._env:
            container_spec["envVars"] = self._env

        resources: dict = {}
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
                {
                    "name": vol_name,
                    "mountPath": mount_path,
                    "type": "chalkfs",
                }
                for vol_name, mount_path in self._volumes
            ]

        spec: dict = {
            "containerSpec": container_spec,
            "scalingSpec": {
                "minReplicas": self._min_replicas,
                "maxReplicas": self._max_replicas,
            },
        }

        print(f"Deploying scaling group '{sg_name}'...")
        resp = self._scaling_group_rpc(
            "CreateScalingGroup",
            {"spec": spec},
            timeout=60,
        )
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
                poll_interval=poll_interval,
                timeout=ready_timeout,
            )

        return self

    def _poll_until_running(
        self,
        *,
        poll_interval: float,
        timeout: float,
    ) -> None:
        assert self._scaling_group_id is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            resp = self._scaling_group_rpc(
                "GetScalingGroup",
                {"id": self._scaling_group_id},
            )
            if resp.status_code != 200:
                raise ScalingGroupError(f"GetScalingGroup failed: {resp.text}")
            info = _parse_scaling_group_response(resp.json()["scalingGroup"])
            self._scaling_group_info = info
            print(f"  poll: status={info.status}")
            if info.status == "Running":
                return
            if info.status in ("Failed", "Error"):
                raise ScalingGroupError(
                    f"Scaling group {self._scaling_group_id} entered "
                    f"{info.status} state"
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
        if self._scaling_group_info is None or self._scaling_group_info.web_url is None:
            raise ScalingGroupError(
                "Scaling group has no web_url. Call .deploy() first or .refresh() to update."
            )
        url = f"{self._scaling_group_info.web_url}{path}"
        return httpx.request(
            method,
            url,
            json=json,
            content=data,
            headers=headers,
            timeout=timeout,
        )

    def delete(self) -> None:
        """Delete the scaling group and clean up volumes."""
        self._ensure_auth()
        if self._scaling_group_id is not None:
            self._scaling_group_rpc(
                "DeleteScalingGroup", {"id": self._scaling_group_id}
            )
            self._scaling_group_id = None
            self._scaling_group_info = None

        for vol_name, _ in self._volumes:
            self._volume_rpc("DeleteVolume", {"name": vol_name})
        self._volumes = []

    def refresh(self) -> ScalingGroupInfo:
        """Fetch latest scaling group status from the server."""
        if self._scaling_group_id is None:
            raise ScalingGroupError("No scaling group ID — call .deploy() first.")
        self._ensure_auth()
        resp = self._scaling_group_rpc(
            "GetScalingGroup", {"id": self._scaling_group_id}
        )
        if resp.status_code != 200:
            raise ScalingGroupError(f"GetScalingGroup failed: {resp.text}")
        self._scaling_group_info = _parse_scaling_group_response(
            resp.json()["scalingGroup"]
        )
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
            self._scaling_group_info.status
            if self._scaling_group_info
            else "not deployed"
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
