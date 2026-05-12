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
from typing import TYPE_CHECKING

from chalkcompute._connect import ConnectClient, ConnectError, build_image
from chalkcompute._container import build_chalk_container_spec
from chalkcompute._gen.chalk.scalinggroup.v1 import (
    service_connect as scaling_connect,
)
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb2 as scaling_pb
from chalkcompute._gen.chalk.volume.v1 import volume_connect, volume_pb2
from chalkcompute._naming import sanitize_dns_label, validate_dns_label
from chalkcompute._tracing import traced
from chalkcompute._volume import VolumeClient, upload_lazy_files

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import httpx

    from chalkcompute._image import Image as _Image
    from chalkcompute._secret import Secret as _Secret

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
    ) -> None:
        self._image = image
        if name is not None:
            validate_dns_label(name)
        self._name = name
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        self._env = env or {}
        self._secrets: list[_Secret] = list(secrets or [])
        self._volumes: list[tuple[str, str]] = list(volumes or [])
        self._entrypoint = entrypoint
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

        self._client = ConnectClient()

    # -- Alternate constructors ------------------------------------------------

    @classmethod
    def from_id(cls, scaling_group_id: str) -> ScalingGroup:
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
        sg = cls._make_empty()
        try:
            resp = sg._client.unary(
                scaling_connect.ScalingGroupManagerServiceClientSync,
                "get_scaling_group",
                scaling_pb.GetScalingGroupRequest(id=scaling_group_id),
            )
        except ConnectError as e:
            raise ScalingGroupError(f"GetScalingGroup failed: {e}") from e
        sg._scaling_group_info = _scaling_group_info_from_proto(resp.scaling_group)
        sg._scaling_group_id = sg._scaling_group_info.id
        return sg

    @classmethod
    def from_name(cls, name: str) -> ScalingGroup:
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
        sg = cls._make_empty()
        try:
            resp = sg._client.unary(
                scaling_connect.ScalingGroupManagerServiceClientSync,
                "get_scaling_group",
                scaling_pb.GetScalingGroupRequest(name=name),
            )
        except ConnectError as e:
            raise ScalingGroupError(f"GetScalingGroup failed: {e}") from e
        sg._scaling_group_info = _scaling_group_info_from_proto(resp.scaling_group)
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
        sg._secrets = []
        sg._volumes = []
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
        sg._client = ConnectClient()
        return sg

    # -- Lifecycle -------------------------------------------------------------

    @traced("chalkcompute.scaling_group.deploy")
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

        # 2. Upload lazy local files to volumes (if any)
        lazy_files = (
            self._image.lazy_local_files if isinstance(self._image, Image) else set()
        )
        if lazy_files:
            logger.info("Uploading %d local file(s) to volume(s)...", len(lazy_files))
            assert isinstance(self._image, Image)
            self._volumes += upload_lazy_files(
                VolumeClient.from_connect(self._client),
                self._image,
                volume_prefix="sg-files",
            )
            for vn, mp in self._volumes:
                logger.info("  Volume created: %s (mount: %s)", vn, mp)

        # 3. Build the ScalingGroupSpec
        sg_name = sanitize_dns_label(self._name or f"sg-{uuid.uuid4().hex[:8]}")
        if self._secrets:
            from chalkcompute._secret import resolve_lazy_secrets

            if any(s.is_lazy for s in self._secrets):
                resolved, _ = resolve_lazy_secrets(self._secrets, self._client)
                self._secrets = resolved

        assert self._built_image_uri is not None
        container_spec = build_chalk_container_spec(
            name=sg_name,
            image=self._built_image_uri,
            entrypoint=self._entrypoint,
            env=self._env,
            port=self._port,
            protocol=self._protocol,
            cpu=self._cpu,
            memory=self._memory,
            gpu=self._gpu,
            volumes=self._volumes,
            secrets=self._secrets,
        )

        scaling_spec = scaling_pb.ScalingSpec(
            min_replicas=self._min_replicas,
            max_replicas=self._max_replicas,
        )
        if self._shutdown_delay_seconds is not None:
            scaling_spec.shutdown_delay_seconds = self._shutdown_delay_seconds
        if self._scaling_interval_seconds is not None:
            scaling_spec.window_seconds = self._scaling_interval_seconds
        if self._target_cpu_utilization_percentage is not None:
            scaling_spec.target_cpu_utilization_percentage = (
                self._target_cpu_utilization_percentage
            )

        logger.info("Deploying scaling group '%s'...", sg_name)
        try:
            resp = self._client.unary(
                scaling_connect.ScalingGroupManagerServiceClientSync,
                "create_scaling_group",
                scaling_pb.CreateScalingGroupRequest(
                    spec=scaling_pb.ScalingGroupSpec(
                        container_spec=container_spec,
                        scaling_spec=scaling_spec,
                    )
                ),
                timeout=60,
            )
        except ConnectError as e:
            raise ScalingGroupError(f"CreateScalingGroup failed: {e}") from e

        self._scaling_group_info = _scaling_group_info_from_proto(resp.scaling_group)
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
            try:
                resp = self._client.unary(
                    scaling_connect.ScalingGroupManagerServiceClientSync,
                    "get_scaling_group",
                    scaling_pb.GetScalingGroupRequest(id=self._scaling_group_id),
                )
            except ConnectError as e:
                raise ScalingGroupError(f"GetScalingGroup failed: {e}") from e
            info = _scaling_group_info_from_proto(resp.scaling_group)
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

    @traced("chalkcompute.scaling_group.call")
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

    @traced("chalkcompute.scaling_group.delete")
    def delete(self) -> None:
        """Delete the scaling group and clean up associated volumes."""
        if self._scaling_group_id is not None:
            self._client.unary(
                scaling_connect.ScalingGroupManagerServiceClientSync,
                "delete_scaling_group",
                scaling_pb.DeleteScalingGroupRequest(id=self._scaling_group_id),
            )
            self._scaling_group_id = None
            self._scaling_group_info = None

        for vol_name, _ in self._volumes:
            self._client.unary(
                volume_connect.VolumeServiceClientSync,
                "delete_volume",
                volume_pb2.DeleteVolumeRequest(name=vol_name),
            )
        self._volumes = []

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
        try:
            resp = self._client.unary(
                scaling_connect.ScalingGroupManagerServiceClientSync,
                "get_scaling_group",
                scaling_pb.GetScalingGroupRequest(id=self._scaling_group_id),
            )
        except ConnectError as e:
            raise ScalingGroupError(f"GetScalingGroup failed: {e}") from e
        self._scaling_group_info = _scaling_group_info_from_proto(resp.scaling_group)
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


def _scaling_group_info_from_proto(
    data: scaling_pb.ScalingGroupResponse,
) -> ScalingGroupInfo:
    return ScalingGroupInfo(
        id=data.id,
        name=data.name,
        status=data.status,
        web_url=data.web_url if data.HasField("web_url") else None,
        ready_replicas=data.ready_replicas,
        available_replicas=data.available_replicas,
    )
