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

import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chalkcompute._connect import ConnectClient, build_image
from chalkcompute._naming import sanitize_dns_label, validate_dns_label, validate_env
from chalkcompute._tracing import _instrumented
from chalkcompute._volume import (
    VolumeDriver,
    VolumeError,
    VolumeMount,
    cleanup_owned_volumes,
    resolve_referenced_volume_type,
)
from chalkcompute._vvolume import VersionedVolumeClient

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import httpx

    from chalkcompute._image import Image as _Image
    from chalkcompute._secret import Secret as _Secret

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
    """Opaque scaling-group identifier assigned by the service."""

    name: str
    """DNS-safe scaling-group name."""

    status: str
    """Lifecycle state reported by the service (e.g. ``"Running"``)."""

    web_url: str | None = None
    """URL to which HTTP traffic should be sent, if ready."""

    ready_replicas: int = 0
    """Number of replicas currently ready to serve traffic."""

    available_replicas: int = 0
    """Number of replicas available behind the service endpoint."""


# ---------------------------------------------------------------------------
# ScalingGroup
# ---------------------------------------------------------------------------


class ScalingGroup:
    """A managed scaling group backed by ``ScalingGroupManagerService``.

    Orchestrates image building, local file upload via volumes, and
    scaling-group lifecycle in a single high-level API.

    Examples
    --------
    >>> from chalkcompute import ScalingGroup, Image
    >>> img = (
    ...     Image.debian_slim()
    ...     .pip_install(["flask"])
    ...     .add_local_file("./app.py", "/app/app.py")
    ...     .entrypoint(["python", "/app/app.py"])
    ... )
    >>> sg = ScalingGroup(image=img, port=8080).deploy().wait_ready()
    >>> resp = sg.call("/health", method="GET")
    >>> sg.delete()
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
        secrets: list[_Secret] | None = None,
        volumes: list[tuple[str, str]] | None = None,
        entrypoint: list[str] | None = None,
        port: int = 8080,
        protocol: str = "http",
        min_replicas: int = 1,
        max_replicas: int = 1,
        shutdown_delay_seconds: int | None = None,
        scaling_interval_seconds: int | None = None,
        target_cpu_utilization_percentage: int | None = None,
        chalk_client: Any | None = None,
    ) -> None:
        self._image = image
        if name is not None:
            validate_dns_label(name)
        self._name = name
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        validate_env(env)
        self._env = env or {}
        self._secrets: list[_Secret] = list(secrets or [])
        # User-referenced volumes carry no type of their own; their backend
        # (and thus mount type) is resolved at deploy time via
        # resolve_referenced_volume_type. Store the raw (name, mount_path)
        # references here and never default their type.
        self._referenced_volumes: list[tuple[str, str]] = [
            (name, mount_path) for name, mount_path in (volumes or [])
        ]
        # Populated at deploy time: referenced volumes (typed by lookup) plus
        # any freshly uploaded volumes (typed by the upload driver).
        self._volumes: list[VolumeMount] = []
        self._volume_driver: VolumeDriver | None = None
        self._entrypoint = entrypoint
        if not (1 <= port <= 65535):
            raise ValueError(f"port must be in range 1–65535, got {port}")
        self._port = port
        self._protocol = protocol
        self._min_replicas = min_replicas
        self._max_replicas = max_replicas
        self._shutdown_delay_seconds = shutdown_delay_seconds
        self._scaling_interval_seconds = scaling_interval_seconds
        self._target_cpu_utilization_percentage = target_cpu_utilization_percentage

        # Set after .deploy()
        self._built_image_uri: str | None = None
        self._scaling_group_id: str | None = None
        self._scaling_group_info: ScalingGroupInfo | None = None

        self._client = ConnectClient(chalk_client=chalk_client)

    # -- Alternate constructors ------------------------------------------------

    @classmethod
    def from_id(
        cls, scaling_group_id: str, *, chalk_client: Any | None = None
    ) -> ScalingGroup:
        """Attach to an existing scaling group by ID.

        Parameters
        ----------
        scaling_group_id
            Opaque scaling-group identifier assigned by the service.

        Returns
        -------
        ScalingGroup
            A ``ScalingGroup`` handle bound to the existing resource.

        Raises
        ------
        ScalingGroupError
            If the scaling group cannot be found.
        """
        sg = cls._make_empty(chalk_client=chalk_client)
        resp = sg._client.rpc(_SVC, "GetScalingGroup", {"id": scaling_group_id})
        if resp.status_code != 200:
            raise ScalingGroupError(f"GetScalingGroup failed: {resp.text}")
        sg._scaling_group_info = _parse_scaling_group_response(
            resp.json()["scalingGroup"]
        )
        sg._scaling_group_id = sg._scaling_group_info.id
        return sg

    @classmethod
    def from_name(cls, name: str, *, chalk_client: Any | None = None) -> ScalingGroup:
        """Attach to an existing scaling group by name.

        Parameters
        ----------
        name
            DNS-safe scaling-group name.

        Returns
        -------
        ScalingGroup
            A ``ScalingGroup`` handle bound to the existing resource.

        Raises
        ------
        ScalingGroupError
            If the scaling group cannot be found.
        """
        sg = cls._make_empty(chalk_client=chalk_client)
        resp = sg._client.rpc(_SVC, "GetScalingGroup", {"name": name})
        if resp.status_code != 200:
            raise ScalingGroupError(f"GetScalingGroup failed: {resp.text}")
        sg._scaling_group_info = _parse_scaling_group_response(
            resp.json()["scalingGroup"]
        )
        sg._scaling_group_id = sg._scaling_group_info.id
        sg._name = name
        return sg

    @classmethod
    def _make_empty(cls, *, chalk_client: Any | None = None) -> ScalingGroup:
        sg = cls.__new__(cls)
        sg._image = None  # type: ignore[assignment]
        sg._name = None
        sg._cpu = None
        sg._memory = None
        sg._gpu = None
        sg._env = {}
        sg._secrets = []
        sg._referenced_volumes = []
        sg._volumes = []
        sg._volume_driver = None
        sg._entrypoint = None
        sg._port = 8080
        sg._protocol = "http"
        sg._min_replicas = 1
        sg._max_replicas = 1
        sg._shutdown_delay_seconds = None
        sg._scaling_interval_seconds = None
        sg._target_cpu_utilization_percentage = None
        sg._built_image_uri = None
        sg._scaling_group_id = None
        sg._scaling_group_info = None
        sg._client = ConnectClient(chalk_client=chalk_client)
        return sg

    # -- Lifecycle -------------------------------------------------------------

    @_instrumented("chalkcompute.scaling_group.deploy")
    def deploy(
        self,
        *,
        poll_interval: float = 0.5,
        build_timeout: float = 600.0,
        ready_timeout: float = 300.0,
    ) -> ScalingGroup:
        """Build the image, upload local files, and create the scaling group.

        Parameters
        ----------
        poll_interval
            Seconds between status polls for both build and startup.
        build_timeout
            Maximum seconds to wait for the image build to complete.
        ready_timeout
            Maximum seconds to wait for the scaling group to reach ``Running``.

        Returns
        -------
        ScalingGroup
            ``self``, to allow chaining.

        Raises
        ------
        ScalingGroupError
            If deployment fails or times out.
        """
        from chalkcompute._image import Image

        # 1. Resolve image URI
        if isinstance(self._image, Image):
            logger.info("Building image from %r...", self._image)
            self._built_image_uri = build_image(
                self._client,
                self._image,
                poll_interval=poll_interval,
                timeout=build_timeout,
            )
            logger.info("Image built: %s", self._built_image_uri)
        else:
            self._built_image_uri = self._image

        # 2a. Resolve the real mount type of each user-referenced volume from
        # its backend, instead of defaulting. A missing volume fails the deploy.
        self._volumes = [
            VolumeMount(
                name=name,
                mount_path=mount_path,
                type=resolve_referenced_volume_type(self._client, name),
            )
            for name, mount_path in self._referenced_volumes
        ]

        # 2b. Upload lazy local files to volumes (if any)
        lazy_files = (
            self._image.lazy_local_files if isinstance(self._image, Image) else set()
        )
        if lazy_files:
            logger.info("Uploading %d local file(s) to volume(s)...", len(lazy_files))
            assert isinstance(self._image, Image)
            driver = VersionedVolumeClient.from_connect(self._client)
            self._volume_driver = driver
            self._volumes += driver.upload_lazy_files(
                self._image,
                volume_prefix="sg-files",
            )
            for v in self._volumes:
                logger.info("  Volume created: %s (mount: %s)", v.name, v.mount_path)

        # 3. Build the ScalingGroupSpec
        sg_name = sanitize_dns_label(self._name or f"sg-{uuid.uuid4().hex[:8]}")
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

        if self._secrets:
            from chalkcompute._secret import resolve_lazy_secrets

            if any(s.is_lazy for s in self._secrets):
                resolved, _ = resolve_lazy_secrets(self._secrets, self._client)
                self._secrets = resolved
            container_spec["secretRefs"] = [s._to_proto_dict() for s in self._secrets]

        if self._volumes:
            for v in self._volumes:
                if not v.type:
                    raise VolumeError(
                        f"Volume {v.name!r} mounted at {v.mount_path!r} has no "
                        "mount type; refusing to guess one for the container spec"
                    )
            container_spec["volumes"] = [v.to_spec_dict() for v in self._volumes]

        scaling_spec: dict[str, Any] = {
            "minReplicas": self._min_replicas,
            "maxReplicas": self._max_replicas,
        }
        if self._shutdown_delay_seconds is not None:
            scaling_spec["shutdownDelaySeconds"] = self._shutdown_delay_seconds
        if self._scaling_interval_seconds is not None:
            scaling_spec["windowSeconds"] = self._scaling_interval_seconds
        if self._target_cpu_utilization_percentage is not None:
            scaling_spec["targetCpuUtilizationPercentage"] = (
                self._target_cpu_utilization_percentage
            )

        spec: dict[str, Any] = {
            "containerSpec": container_spec,
            "scalingSpec": scaling_spec,
        }

        logger.info("Deploying scaling group '%s'...", sg_name)
        resp = self._client.rpc(_SVC, "CreateScalingGroup", {"spec": spec}, timeout=60)
        if resp.status_code != 200:
            raise ScalingGroupError(f"CreateScalingGroup failed: {resp.text}")

        data = resp.json()
        self._scaling_group_info = _parse_scaling_group_response(data["scalingGroup"])
        self._scaling_group_id = self._scaling_group_info.id
        logger.info(
            "Scaling group created: %s (status=%s)",
            self._scaling_group_id,
            self._scaling_group_info.status,
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
            resp = self._client.rpc(
                _SVC, "GetScalingGroup", {"id": self._scaling_group_id}
            )
            if resp.status_code != 200:
                raise ScalingGroupError(f"GetScalingGroup failed: {resp.text}")
            info = _parse_scaling_group_response(resp.json()["scalingGroup"])
            self._scaling_group_info = info
            logger.info("  poll: status=%s", info.status)
            if info.status in ("Running", "Available"):
                return
            if info.status in ("Failed", "Error"):
                raise ScalingGroupError(
                    f"Scaling group {self._scaling_group_id} entered {info.status} state"
                )
            time.sleep(poll_interval)
        raise ScalingGroupError(
            f"Scaling group {self._scaling_group_id} timed out after {timeout}s"
        )

    @_instrumented("chalkcompute.scaling_group.call")
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
        """Make an HTTP request to the scaling group's endpoint.

        Parameters
        ----------
        path
            URL path appended to the scaling group's ``web_url``.
        method
            HTTP method.
        json
            Optional JSON-serializable body.
        data
            Optional raw bytes body (mutually exclusive with ``json``).
        headers
            Optional HTTP headers.
        timeout
            Request timeout in seconds.

        Returns
        -------
        httpx.Response
            The HTTP response.

        Raises
        ------
        ScalingGroupError
            If the scaling group has no ``web_url`` yet.
        """
        import httpx

        if self._scaling_group_info is None or self._scaling_group_info.web_url is None:
            raise ScalingGroupError(
                "Scaling group has no web_url. Call .deploy() first or .refresh() to update."
            )
        url = f"{self._scaling_group_info.web_url}{path}"
        return httpx.request(
            method, url, json=json, content=data, headers=headers, timeout=timeout
        )

    def wait_ready(
        self,
        *,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
    ) -> ScalingGroup:
        """Wait until a running scaling group has at least one ready replica.

        Parameters
        ----------
        timeout
            Maximum seconds to wait before giving up.
        poll_interval
            Seconds between status polls.

        Returns
        -------
        ScalingGroup
            ``self``, to allow chaining.

        Raises
        ------
        ScalingGroupError
            If the group enters a terminal failed state or never becomes ready
            before the timeout elapses.
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

    @_instrumented("chalkcompute.scaling_group.delete")
    def delete(self) -> None:
        """Delete the scaling group and clean up associated volumes."""
        if self._scaling_group_id is not None:
            self._client.rpc(_SVC, "DeleteScalingGroup", {"id": self._scaling_group_id})
            self._scaling_group_id = None
            self._scaling_group_info = None

        if self._volumes:
            # Driver is used here only to perform the delete action, not to
            # derive a mount type.
            driver = self._volume_driver or VersionedVolumeClient.from_connect(
                self._client
            )
            cleanup_owned_volumes(driver, self._volumes, self._referenced_volumes)
        self._volumes = []
        self._volume_driver = None

    def refresh(self) -> ScalingGroupInfo:
        """Fetch latest scaling-group status from the server.

        Returns
        -------
        ScalingGroupInfo
            The latest metadata for the scaling group.

        Raises
        ------
        ScalingGroupError
            If no scaling-group ID is set (``deploy()`` was never called).
        """
        if self._scaling_group_id is None:
            raise ScalingGroupError("No scaling group ID — call .deploy() first.")
        resp = self._client.rpc(_SVC, "GetScalingGroup", {"id": self._scaling_group_id})
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
