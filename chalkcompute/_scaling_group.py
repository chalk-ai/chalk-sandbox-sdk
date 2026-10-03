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
    print(resp.status)
    sg.delete()
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf import Oneof
from protobuf.wkt import FieldMask

from chalkcompute._logging import _StatusLogger
from chalkcompute._connect import ConnectClient, build_image
from chalkcompute._containerspec import ContainerSpec, validate_replica_and_utilization
from chalkcompute._env import normalize_env
from chalkcompute._deployment_proto import scaling_spec as build_scaling_spec
from chalkcompute._gen.chalk.scalinggroup.v1 import service_connect as sg_connect
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._http import HTTPResponse, SYNC_HTTP_CLIENT
from chalkcompute._naming import sanitize_dns_label
from chalkcompute._image_build import normalize_image_volumes
from chalkcompute._secret import SecretScope, resolve_lazy_secrets
from chalkcompute._remote_function_tracing import (
    TracingPolicyConfig,
    merge_workload_tracing_env,
    normalize_workload_tracing,
)
from chalkcompute._tracing import _instrumented
from chalkcompute._volume import (
    Volume,
    VolumeClient,
    VolumeMount,
    cleanup_owned_volumes,
    referenced_volume_mount,
)

logger = logging.getLogger(__name__)

_OWNED_VOLUME_PREFIX = "chalkcompute-sg-files"


class ScaleFromZeroRequestPolicy(Enum):
    ERROR = "ERROR"
    HOLD = "HOLD"


if TYPE_CHECKING:
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


@dataclass(frozen=True)
class ReadinessProbe:
    """Continuous readiness check for a scaling-group replica."""

    _kind: Literal["http", "grpc"]
    _value: str | None
    period_seconds: int | None = None
    timeout_seconds: int | None = None
    failure_threshold: int | None = None

    @classmethod
    def http(
        cls,
        path: str | None = None,
        *,
        period_seconds: int | None = None,
        timeout_seconds: int | None = None,
        failure_threshold: int | None = None,
    ) -> ReadinessProbe:
        """Create an HTTP GET readiness probe."""
        return cls(
            _kind="http",
            _value=path,
            period_seconds=period_seconds,
            timeout_seconds=timeout_seconds,
            failure_threshold=failure_threshold,
        )

    @classmethod
    def grpc(
        cls,
        service: str | None = None,
        *,
        period_seconds: int | None = None,
        timeout_seconds: int | None = None,
        failure_threshold: int | None = None,
    ) -> ReadinessProbe:
        """Create a standard gRPC health-check readiness probe."""
        return cls(
            _kind="grpc",
            _value=service,
            period_seconds=period_seconds,
            timeout_seconds=timeout_seconds,
            failure_threshold=failure_threshold,
        )

    def to_dict(self) -> dict[str, Any]:
        check = (
            {}
            if self._value is None
            else {"path" if self._kind == "http" else "service": self._value}
        )
        body: dict[str, Any] = {self._kind: check}
        if self.period_seconds is not None:
            body["periodSeconds"] = self.period_seconds
        if self.timeout_seconds is not None:
            body["timeoutSeconds"] = self.timeout_seconds
        if self.failure_threshold is not None:
            body["failureThreshold"] = self.failure_threshold
        return body


@dataclass(frozen=True)
class StartupProbe:
    """One-time check of the externally routed endpoint before deploy is ready."""

    _kind: Literal["http", "grpc"]
    _value: str | None

    @classmethod
    def http(cls, path: str | None = None) -> StartupProbe:
        """Create an HTTP GET startup probe. An unset path uses ``/``."""
        return cls(_kind="http", _value=path)

    @classmethod
    def grpc(cls, method: str | None = None) -> StartupProbe:
        """Create a gRPC startup probe.

        An explicit ``method`` must be fully qualified and return OK. An unset
        method calls ``/chalk.health.v1.Probe/Check`` and expects UNIMPLEMENTED
        to confirm that the routed endpoint speaks gRPC.
        """
        return cls(_kind="grpc", _value=method)

    def to_dict(self) -> dict[str, Any]:
        check = (
            {}
            if self._value is None
            else {"path" if self._kind == "http" else "method": self._value}
        )
        return {self._kind: check}


@dataclass(frozen=True)
class CronScalingWindow:
    """A single cron-based scaling window."""

    start: str
    """Cron expression for window start (e.g. ``"0 8 * * 1-5"``)."""

    end: str
    """Cron expression for window end (e.g. ``"0 18 * * 1-5"``)."""

    desired_replicas: int
    """Replica floor during the active window. ``0`` allows scale-to-zero."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "desiredReplicas": self.desired_replicas,
        }


@dataclass(frozen=True)
class CronScalingTrigger:
    """Uses KEDA cron triggers to set a replica floor during specified time windows."""

    timezone: str
    """IANA timezone for evaluating cron expressions (e.g. ``"America/New_York"``)."""

    windows: list[CronScalingWindow]
    """The cron windows and their replica floors."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "timezone": self.timezone,
            "windows": [w.to_dict() for w in self.windows],
        }


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

    current_revision_id: str | None = None
    """Immutable revision currently selected to serve traffic."""


@dataclass(frozen=True)
class ScalingGroupRevisionInfo:
    """Metadata for one immutable scaling-group revision."""

    id: str
    scaling_group_id: str
    scaling_group_name: str
    status: str
    status_message: str | None = None
    created_at: datetime | None = None
    deleted_at: datetime | None = None
    is_current: bool = False
    status_details: str | None = None


# ---------------------------------------------------------------------------
# ScalingGroup
# ---------------------------------------------------------------------------


class ScalingGroup:
    """A managed scaling group backed by ``ScalingGroupManagerService``.

    Orchestrates image building, local file upload via volumes, and
    scaling-group lifecycle in a single high-level API.

    ``readiness_probe`` checks each replica directly. Optional ``startup_probe``
    checks the externally routed endpoint before deployment becomes ready, e.g.
    ``StartupProbe.http("/health")``. Use ``protocol="grpc"`` with
    ``StartupProbe.grpc(...)``. External startup probes are not supported with
    ``authenticated=True``.

    Examples
    --------
    >>> from chalkcompute import ScalingGroup, Image
    >>> img = (
    ...     Image.debian_slim()
    ...     .pip_install(["flask"])
    ...     .add_local_file("./app.py", "/app/app.py")
    ...     .entrypoint(["python", "/app/app.py"])
    ... )
    >>> sg = ScalingGroup(
    ...     image=img,
    ...     port=8080,
    ...     authenticated=True,
    ... ).deploy().wait_ready()
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
        env: Mapping[str, str | _Secret] | None = None,
        secrets: list[_Secret] | None = None,
        volumes: list[tuple[str | Volume, str] | Volume | VolumeMount] | None = None,
        entrypoint: list[str] | None = None,
        port: int | None = 8080,
        protocol: str = "http",
        authenticated: bool = False,
        min_replicas: int = 1,
        max_replicas: int = 1,
        readiness_probe: ReadinessProbe | None = None,
        startup_probe: StartupProbe | None = None,
        shutdown_delay_seconds: int | None = None,
        scaling_interval_seconds: int | None = None,
        target_cpu_utilization_percentage: int | None = None,
        target_gpu_utilization_percentage: int | None = None,
        cron_scaling_trigger: CronScalingTrigger | None = None,
        scale_from_zero_request_policy: ScaleFromZeroRequestPolicy | None = None,
        tags: dict[str, str] | None = None,
        chalk_identity: bool = False,
        tracing: TracingPolicyConfig | None = None,
        chalk_client: Any | None = None,
    ) -> None:
        if image is None:
            raise TypeError(
                "image must be an Image or image reference string, got None"
            )
        env, secrets = normalize_env(env, secrets)
        self._tracing = normalize_workload_tracing(tracing)
        env = merge_workload_tracing_env(env, self._tracing)
        normalized_image, resolved_volumes = normalize_image_volumes(image, volumes)
        if normalized_image is None:
            raise TypeError(
                "image must be an Image or image reference string, got None"
            )
        ContainerSpec.validate_fields(
            image=normalized_image,
            name=name,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            env=env,
            entrypoint=entrypoint,
            secrets=secrets,
            port=port,
            tags=tags,
            chalk_identity=chalk_identity,
        )
        validate_replica_and_utilization(
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            gpu=gpu,
            target_cpu_utilization_percentage=target_cpu_utilization_percentage,
            target_gpu_utilization_percentage=target_gpu_utilization_percentage,
        )
        if startup_probe is not None:
            if port is None:
                raise ValueError("startup_probe requires port")
            if startup_probe._kind == "grpc" and protocol != "grpc":
                raise ValueError("StartupProbe.grpc requires protocol='grpc'")
            if startup_probe._kind == "http" and protocol == "grpc":
                raise ValueError("StartupProbe.http requires an HTTP protocol")
        self._image: _Image | str | None = normalized_image
        self._name = name
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        self._env = dict(env) if env else {}
        self._secrets: list[_Secret] = list(secrets or [])
        # Existence is checked at deploy time, not here.
        self._referenced_volumes: list[VolumeMount] = resolved_volumes
        # Populated at deploy time: referenced volumes plus any freshly
        # uploaded volumes.
        self._volumes: list[VolumeMount] = []
        self._owned_volumes: list[VolumeMount] = []
        self._volume_driver: VolumeClient | None = None
        self._entrypoint = list(entrypoint) if entrypoint is not None else None
        self._port = port
        self._protocol = protocol
        self._authenticated = authenticated
        self._min_replicas = min_replicas
        self._max_replicas = max_replicas
        self._readiness_probe = readiness_probe
        self._startup_probe = startup_probe
        self._shutdown_delay_seconds = shutdown_delay_seconds
        self._scaling_interval_seconds = scaling_interval_seconds
        self._target_cpu_utilization_percentage = target_cpu_utilization_percentage
        self._target_gpu_utilization_percentage = target_gpu_utilization_percentage
        self._cron_scaling_trigger = cron_scaling_trigger
        if scale_from_zero_request_policy is not None and not isinstance(
            scale_from_zero_request_policy, ScaleFromZeroRequestPolicy
        ):
            raise TypeError(
                "scale_from_zero_request_policy must be a ScaleFromZeroRequestPolicy"
            )
        self._scale_from_zero_request_policy = scale_from_zero_request_policy
        self._tags = dict(tags or {})
        self._chalk_identity = chalk_identity

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
        response = _get_scaling_group(sg._client, id=scaling_group_id)
        data = response.scaling_group
        if data is None:
            raise ScalingGroupError("GetScalingGroup returned no scaling group")
        sg._scaling_group_info = _parse_scaling_group_response(data)
        sg._authenticated = _scaling_group_response_is_authenticated(data)
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
        response = _get_scaling_group(sg._client, name=name)
        data = response.scaling_group
        if data is None:
            raise ScalingGroupError("GetScalingGroup returned no scaling group")
        sg._scaling_group_info = _parse_scaling_group_response(data)
        sg._authenticated = _scaling_group_response_is_authenticated(data)
        sg._scaling_group_id = sg._scaling_group_info.id
        sg._name = name
        return sg

    @classmethod
    def _make_empty(cls, *, chalk_client: Any | None = None) -> ScalingGroup:
        sg = cls.__new__(cls)
        sg._image = None
        sg._name = None
        sg._cpu = None
        sg._memory = None
        sg._gpu = None
        sg._env = {}
        sg._secrets = []
        sg._referenced_volumes = []
        sg._volumes = []
        sg._owned_volumes = []
        sg._volume_driver = None
        sg._entrypoint = None
        sg._port = None
        sg._protocol = "http"
        sg._authenticated = False
        sg._min_replicas = 1
        sg._max_replicas = 1
        sg._readiness_probe = None
        sg._startup_probe = None
        sg._shutdown_delay_seconds = None
        sg._scaling_interval_seconds = None
        sg._target_cpu_utilization_percentage = None
        sg._target_gpu_utilization_percentage = None
        sg._cron_scaling_trigger = None
        sg._scale_from_zero_request_policy = None
        sg._tags = {}
        sg._chalk_identity = False
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
            Maximum seconds to wait for the scaling group to have at least one
            ready and available replica and pass any configured startup probe.

        Returns
        -------
        ScalingGroup
            ``self``, to allow chaining.

        Raises
        ------
        ScalingGroupError
            If deployment fails or times out.
        """
        from chalkcompute._progress import (
            progress_add,
            progress_done,
            progress_fail,
            progress_finish,
        )

        key = f"scaling_group:{id(self)}"
        progress_add(key, "scaling_group", self._name or "Scaling group")
        try:
            self._deploy(
                poll_interval=poll_interval,
                build_timeout=build_timeout,
                ready_timeout=ready_timeout,
            )
            progress_done(key, detail="ready")
            logger.info("Scaling group ready: %s", self._scaling_group_id)
        except BaseException:
            progress_fail(key)
            raise
        finally:
            progress_finish()
        return self

    def _deploy(
        self, *, poll_interval: float, build_timeout: float, ready_timeout: float
    ) -> ScalingGroup:
        from chalkcompute._image import Image
        from chalkcompute._progress import progress_update

        key = f"scaling_group:{id(self)}"

        # 1. Resolve image URI
        if isinstance(self._image, Image):
            progress_update(key, detail="Building image")
            logger.info("Building image from %r...", self._image)
            self._built_image_uri = build_image(
                self._client,
                self._image,
                poll_interval=poll_interval,
                timeout=build_timeout,
            )
            logger.info("Image built: %s", self._built_image_uri)
            progress_update(key, detail="Image built")
        else:
            self._built_image_uri = self._image

        sg_name = sanitize_dns_label(self._name or f"sg-{uuid.uuid4().hex[:8]}")

        # 2a. A missing user-referenced volume fails the deploy.
        self._volumes = [
            referenced_volume_mount(self._client, mount)
            for mount in self._referenced_volumes
        ]

        # 2b. Upload lazy local files to volumes (if any)
        lazy_files = (
            self._image.lazy_local_files if isinstance(self._image, Image) else set()
        )
        if lazy_files:
            progress_update(key, detail=f"Uploading {len(lazy_files)} local file(s)")
            logger.info("Uploading %d local file(s) to volume(s)...", len(lazy_files))
            assert isinstance(self._image, Image)
            driver = VolumeClient.from_connect(self._client)
            self._volume_driver = driver
            uploaded_volumes = driver.upload_lazy_files(
                self._image,
                volume_prefix=_OWNED_VOLUME_PREFIX,
            )
            self._volumes += uploaded_volumes
            self._owned_volumes += uploaded_volumes
            for v in self._volumes:
                logger.info("  Volume created: %s (mount: %s)", v.name, v.mount_path)

        # 3. Build the ScalingGroupSpec
        resolved_secrets = self._secrets
        if any(s.is_lazy for s in self._secrets):
            progress_update(key, detail="Resolving secrets")
            scope = SecretScope.scaling_group(sg_name)
            resolved_secrets, _upserted = resolve_lazy_secrets(
                self._secrets, self._client, scope=scope
            )

        assert self._built_image_uri is not None
        container_spec = ContainerSpec(
            name=sg_name,
            image=self._built_image_uri,
            entrypoint=self._entrypoint,
            cpu=self._cpu,
            memory=self._memory,
            gpu=self._gpu,
            env=self._env,
            volumes=self._volumes,
            secret_refs=resolved_secrets,
            port=self._port,
            protocol=self._protocol,
            authentication="AUTHENTICATED" if self._authenticated else None,
            readiness_probe=(
                self._readiness_probe.to_dict()
                if self._readiness_probe is not None
                else None
            ),
            startup_probe=(
                self._startup_probe.to_dict()
                if self._startup_probe is not None
                else None
            ),
            tags=self._tags,
            chalk_identity=self._chalk_identity,
        ).to_proto()

        spec = sg_pb.ScalingGroupSpec(
            container_spec=container_spec,
            scaling_spec=build_scaling_spec(
                min_replicas=self._min_replicas,
                max_replicas=self._max_replicas,
                shutdown_delay_seconds=self._shutdown_delay_seconds,
                window_seconds=self._scaling_interval_seconds,
                target_cpu_utilization_percentage=(
                    self._target_cpu_utilization_percentage
                ),
                target_gpu_utilization_percentage=(
                    self._target_gpu_utilization_percentage
                ),
                cron_scaling_trigger=self._cron_scaling_trigger,
                scale_from_zero_request_policy=self._scale_from_zero_request_policy,
            ),
        )

        logger.info("Deploying scaling group '%s'...", sg_name)
        progress_update(key, label=f'Scaling group "{sg_name}"', detail="Deploying")
        stub = self._client.stub(sg_connect.ScalingGroupManagerServiceClientSync)
        try:
            if self._scaling_group_id is None:
                response = self._client.call(
                    stub.create_scaling_group,
                    sg_pb.CreateScalingGroupRequest(spec=spec),
                    timeout=60,
                )
                operation = "CreateScalingGroup"
            else:
                response = self._client.call(
                    stub.update_scaling_group,
                    sg_pb.UpdateScalingGroupRequest(
                        scaling_group_id=self._scaling_group_id,
                        spec=spec,
                        update_mask=FieldMask(paths=["spec"]),
                    ),
                    timeout=60,
                )
                operation = "UpdateScalingGroup"
        except ConnectRpcError as exc:
            operation = (
                "UpdateScalingGroup"
                if self._scaling_group_id is not None
                else "CreateScalingGroup"
            )
            raise ScalingGroupError(f"{operation} failed: {exc}") from exc
        if response.scaling_group is None:
            raise ScalingGroupError(f"{operation} returned no scaling group")
        self._scaling_group_info = _parse_scaling_group_response(response.scaling_group)
        self._scaling_group_id = self._scaling_group_info.id
        logger.info(
            "Scaling group deployed: %s (status=%s)",
            self._scaling_group_id,
            self._scaling_group_info.status,
        )

        # 4. Poll until the backing pod is actually ready to serve.
        if not _scaling_group_info_is_ready(
            self._scaling_group_info,
            require_ready_replicas=self._min_replicas > 0,
        ):
            progress_update(key, detail="Waiting for readiness")
            self._poll_until_ready(
                poll_interval=poll_interval,
                timeout=ready_timeout,
                require_ready_replicas=self._min_replicas > 0,
            )

        return self

    def revisions(
        self,
        *,
        limit: int | None = None,
        include_deleted: bool = False,
    ) -> list[ScalingGroupRevisionInfo]:
        """List immutable revisions for this scaling group."""
        if self._scaling_group_id is None:
            raise ScalingGroupError("No scaling group ID — call .deploy() first.")
        if limit is not None and limit < 1:
            raise ValueError(f"limit must be positive, got {limit}")

        stub = self._client.stub(sg_connect.ScalingGroupManagerServiceClientSync)
        revisions: list[ScalingGroupRevisionInfo] = []
        cursor: str | None = None
        while limit is None or len(revisions) < limit:
            page_limit = 100 if limit is None else min(100, limit - len(revisions))
            try:
                response = self._client.call(
                    stub.list_scaling_group_revisions,
                    sg_pb.ListScalingGroupRevisionsRequest(
                        scaling_group_key=Oneof(
                            "scaling_group_id", self._scaling_group_id
                        ),
                        cursor=cursor,
                        limit=page_limit,
                        include_deleted=include_deleted,
                    ),
                )
            except ConnectRpcError as exc:
                raise ScalingGroupError(
                    f"ListScalingGroupRevisions failed: {exc}"
                ) from exc
            revisions.extend(
                _parse_scaling_group_revision(item) for item in response.revisions
            )
            cursor = response.next_cursor or None
            if cursor is None:
                break
        return revisions[:limit] if limit is not None else revisions

    @_instrumented("chalkcompute.scaling_group.rollback")
    def rollback(
        self,
        revision_id: str,
        *,
        wait_ready: bool = True,
        timeout: float = 300.0,
        poll_interval: float = 0.5,
    ) -> ScalingGroup:
        """Select an existing immutable revision without creating a new one."""
        if self._scaling_group_id is None:
            raise ScalingGroupError("No scaling group ID — call .deploy() first.")
        if not revision_id:
            raise ValueError("revision_id must not be empty")

        stub = self._client.stub(sg_connect.ScalingGroupManagerServiceClientSync)
        try:
            response = self._client.call(
                stub.update_scaling_group,
                sg_pb.UpdateScalingGroupRequest(
                    scaling_group_id=self._scaling_group_id,
                    traffic=sg_pb.ScalingGroupTraffic(
                        targets=[
                            sg_pb.ScalingGroupTrafficTarget(
                                revision_target=Oneof(
                                    "scaling_group_revision_id", revision_id
                                ),
                                percent=100,
                            )
                        ]
                    ),
                    update_mask=FieldMask(paths=["traffic"]),
                ),
                timeout=60,
            )
        except ConnectRpcError as exc:
            raise ScalingGroupError(f"UpdateScalingGroup failed: {exc}") from exc
        if response.scaling_group is None:
            raise ScalingGroupError("UpdateScalingGroup returned no scaling group")
        self._scaling_group_info = _parse_scaling_group_response(response.scaling_group)
        if wait_ready:
            self._poll_until_ready(
                poll_interval=poll_interval,
                timeout=timeout,
                require_ready_replicas=self._min_replicas > 0,
            )
        return self

    def _poll_until_ready(
        self,
        *,
        poll_interval: float,
        timeout: float,
        require_ready_replicas: bool = True,
    ) -> None:
        assert self._scaling_group_id is not None
        deadline = time.monotonic() + timeout
        info = self._scaling_group_info
        status_log = _StatusLogger(
            logger,
            f"Scaling group {self._scaling_group_id}",
            progress_key=f"scaling_group:{id(self)}",
        )
        while time.monotonic() < deadline:
            stub = self._client.stub(sg_connect.ScalingGroupManagerServiceClientSync)
            try:
                response = self._client.call(
                    stub.get_scaling_group,
                    sg_pb.GetScalingGroupRequest(id=self._scaling_group_id),
                )
            except ConnectRpcError as exc:
                raise ScalingGroupError(f"get_scaling_group failed: {exc}") from exc
            if response.scaling_group is None:
                raise ScalingGroupError("GetScalingGroup returned no scaling group")
            info = _parse_scaling_group_response(response.scaling_group)
            self._scaling_group_info = info
            status_log.update(
                info.status,
                ready=info.ready_replicas,
                available=info.available_replicas,
                message=response.scaling_group.status_message or "",
            )
            if _scaling_group_info_is_ready(
                info,
                require_ready_replicas=require_ready_replicas,
            ):
                return
            if info.status in ("Failed", "Error"):
                raise ScalingGroupError(
                    f"Scaling group {self._scaling_group_id} entered {info.status} state"
                )
            time.sleep(poll_interval)
        status = info.status if info else "unknown"
        ready = info.ready_replicas if info else 0
        available = info.available_replicas if info else 0
        raise ScalingGroupError(
            f"Scaling group {self._scaling_group_id} not ready after {timeout}s "
            f"(status={status}, ready={ready}, available={available})"
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
    ) -> HTTPResponse[bytes]:
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
            Optional HTTP headers. Authenticated scaling groups automatically
            receive the current Chalk bearer token unless this contains an
            ``Authorization`` header.
        timeout
            Request timeout in seconds.

        Returns
        -------
        HTTPResponse[bytes]
            A buffered HTTP response satisfying the SDK's structural response
            protocol.

        Raises
        ------
        ScalingGroupError
            If the scaling group has no ``web_url`` yet.
        """
        if self._scaling_group_info is None or self._scaling_group_info.web_url is None:
            raise ScalingGroupError(
                "Scaling group has no web_url. Call .deploy() first or .refresh() to update."
            )
        url = f"{self._scaling_group_info.web_url}{path}"
        request_headers = dict(headers) if headers is not None else None
        has_authorization = any(
            key.lower() == "authorization" for key in request_headers or {}
        )
        if self._authenticated and not has_authorization:
            self._client._ensure_auth()
            if not self._client._access_token:
                raise ScalingGroupError("Unable to authenticate scaling-group request.")
            request_headers = {
                **(request_headers or {}),
                "Authorization": f"Bearer {self._client._access_token}",
            }
        return SYNC_HTTP_CLIENT.execute(
            method,
            url,
            headers=request_headers,
            content=data if data is not None else json,
            timeout=timeout,
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
            if _scaling_group_info_is_ready(info):
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
        owned_volumes = {volume.name: volume for volume in self._owned_volumes}
        if self._scaling_group_id is not None:
            try:
                owned_volumes.update(self._owned_volumes_from_revisions())
            except ScalingGroupError as exc:
                logger.warning(
                    "Could not inspect revision volumes before delete: %s", exc
                )
        if self._scaling_group_id is not None:
            stub = self._client.stub(sg_connect.ScalingGroupManagerServiceClientSync)
            self._client.call(
                stub.delete_scaling_group,
                sg_pb.DeleteScalingGroupRequest(id=self._scaling_group_id),
            )
            self._scaling_group_id = None
            self._scaling_group_info = None

        if owned_volumes:
            # Driver is used here only to perform the delete action, not to
            # derive a mount type.
            driver = self._volume_driver or VolumeClient.from_connect(self._client)
            cleanup_owned_volumes(
                driver, owned_volumes.values(), self._referenced_volumes
            )
        self._volumes = []
        self._owned_volumes = []
        self._volume_driver = None

    def _owned_volumes_from_revisions(self) -> dict[str, VolumeMount]:
        """Find SDK-created lazy-file volumes retained by historical revisions."""
        assert self._scaling_group_id is not None
        stub = self._client.stub(sg_connect.ScalingGroupManagerServiceClientSync)
        result: dict[str, VolumeMount] = {}
        cursor: str | None = None
        while True:
            try:
                response = self._client.call(
                    stub.list_scaling_group_revisions,
                    sg_pb.ListScalingGroupRevisionsRequest(
                        scaling_group_key=Oneof(
                            "scaling_group_id", self._scaling_group_id
                        ),
                        cursor=cursor,
                        limit=100,
                        include_deleted=True,
                    ),
                )
            except ConnectRpcError as exc:
                raise ScalingGroupError(
                    f"ListScalingGroupRevisions failed: {exc}"
                ) from exc
            for revision in response.revisions:
                spec = revision.spec
                container_spec = spec.container_spec if spec is not None else None
                if container_spec is None:
                    continue
                for volume in container_spec.volumes:
                    if volume.name.startswith(f"{_OWNED_VOLUME_PREFIX}-"):
                        result[volume.name] = VolumeMount(
                            name=volume.name,
                            mount_path=volume.mount_path,
                            version_id=volume.version_id,
                            ref_name=volume.ref_name,
                        )
            cursor = response.next_cursor or None
            if cursor is None:
                return result

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
        response = _get_scaling_group(self._client, id=self._scaling_group_id)
        if response.scaling_group is None:
            raise ScalingGroupError("GetScalingGroup returned no scaling group")
        self._scaling_group_info = _parse_scaling_group_response(response.scaling_group)
        return self._scaling_group_info

    @property
    def id(self) -> str | None:
        return self._scaling_group_id

    @property
    def info(self) -> ScalingGroupInfo | None:
        return self._scaling_group_info

    @property
    def current_revision_id(self) -> str | None:
        if self._scaling_group_info is None:
            return None
        return self._scaling_group_info.current_revision_id

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


def _get_scaling_group(
    client: ConnectClient, *, id: str | None = None, name: str | None = None
) -> sg_pb.GetScalingGroupResponse:
    stub = client.stub(sg_connect.ScalingGroupManagerServiceClientSync)
    try:
        return client.call(
            stub.get_scaling_group,
            sg_pb.GetScalingGroupRequest(id=id, name=name),
        )
    except ConnectRpcError as exc:
        raise ScalingGroupError(f"GetScalingGroup failed: {exc}") from exc


def _parse_scaling_group_response(data: sg_pb.ScalingGroupResponse) -> ScalingGroupInfo:
    return ScalingGroupInfo(
        id=data.id,
        name=data.name,
        status=data.status,
        web_url=data.web_url or None,
        ready_replicas=data.ready_replicas,
        available_replicas=data.available_replicas,
        current_revision_id=data.revision_id or None,
    )


def _parse_scaling_group_revision(
    data: sg_pb.ScalingGroupRevisionResponse,
) -> ScalingGroupRevisionInfo:
    return ScalingGroupRevisionInfo(
        id=data.id,
        scaling_group_id=data.scaling_group_id,
        scaling_group_name=data.scaling_group_name,
        status=data.status,
        status_message=data.status_message or None,
        status_details=data.status_details or None,
        created_at=None if data.created_at is None else data.created_at.to_datetime(),
        deleted_at=None if data.deleted_at is None else data.deleted_at.to_datetime(),
        is_current=data.latest,
    )


def _scaling_group_response_is_authenticated(data: sg_pb.ScalingGroupResponse) -> bool:
    return (
        data.spec is not None
        and data.spec.container_spec is not None
        and data.spec.container_spec.authentication == "AUTHENTICATED"
    )


def _scaling_group_info_is_ready(
    info: ScalingGroupInfo, *, require_ready_replicas: bool = True
) -> bool:
    if not require_ready_replicas:
        return info.status in ("Running", "Available", "ScaledToZero")
    if info.status not in ("Running", "Available"):
        return False
    return info.ready_replicas > 0 and info.available_replicas > 0
