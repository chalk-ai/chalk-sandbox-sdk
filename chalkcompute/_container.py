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
import ipaddress
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from chalkcompute._connect import ConnectClient
from chalkcompute._naming import sanitize_dns_label, validate_dns_label, validate_env
from chalkcompute._tracing import _instrumented
from chalkcompute._volume import (
    VolumeDriver,
    VolumeError,
    VolumeMount,
    _resolve_driver,
    cleanup_owned_volumes,
    resolve_referenced_volume_type,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from chalkcompute._image import Image as _Image
    from chalkcompute._secret import Secret

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
    """Result of a one-shot command executed inside a container.

    Returned by :meth:`Container.exec`.  All three fields are populated once
    the command has finished — ``ExecResult`` does not stream.
    """

    stdout: bytes
    """Raw bytes written to standard output by the command."""

    stderr: bytes
    """Raw bytes written to standard error by the command."""

    exit_code: int
    """POSIX exit status of the command (``0`` on success, non-zero on
    error; signals are encoded as ``128 + signum`` where applicable)."""

    @property
    def stdout_text(self) -> str:
        """:attr:`stdout` decoded as UTF-8 (with ``errors="replace"``).

        Convenience for the common case where the command emits text.
        Invalid UTF-8 bytes are replaced with ``U+FFFD`` rather than
        raising.
        """
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        """:attr:`stderr` decoded as UTF-8 (with ``errors="replace"``).

        Same UTF-8-with-replacement semantics as :attr:`stdout_text`.
        """
        return self.stderr.decode("utf-8", errors="replace")


class KernelPolicy(Enum):
    RESTRICTED = "RESTRICTED"
    OPEN = "OPEN"


@dataclass
class ContainerSecurityPolicy:
    """Security policy settings for a container.

    A missing policy leaves the container unrestricted for backwards compatibility.
    """

    kernel_policy: KernelPolicy = KernelPolicy.RESTRICTED
    """Kernel hardening policy."""

    def to_dict(self) -> dict[str, Any]:
        return {"kernelPolicy": self.kernel_policy.value}


@dataclass
class PortRange:
    """Inclusive destination port range for a network policy route."""

    start_port: int
    """Inclusive start port. Must be between 1 and 65535."""

    end_port: int | None = None
    """Inclusive end port. Defaults to ``start_port``."""

    def __post_init__(self) -> None:
        if self.end_port is None:
            self.end_port = self.start_port
        _validate_port_range(self.start_port, self.end_port)

    def to_dict(self) -> dict[str, int]:
        assert self.end_port is not None
        return {"startPort": self.start_port, "endPort": self.end_port}


PortRangeInput = PortRange | tuple[int, int] | int


@dataclass(init=False)
class AllowedRoute:
    """Allowed destination CIDR and optional destination port ranges.

    Empty ``port_ranges`` means all destination ports for the route.
    """

    route: str
    """Destination CIDR, e.g. ``"10.0.0.0/8"``."""

    port_ranges: list[PortRange]
    """Destination port ranges allowed for this route."""

    def __init__(
        self,
        route: str | None = None,
        port_ranges: Iterable[PortRangeInput] | None = None,
        *,
        cidr: str | None = None,
        ports: Iterable[PortRangeInput] | None = None,
    ) -> None:
        if route is None:
            route = cidr
        elif cidr is not None and cidr != route:
            raise ValueError("Pass only one of route or cidr")
        if route is None:
            raise ValueError("route is required")
        if port_ranges is not None and ports is not None:
            raise ValueError("Pass only one of port_ranges or ports")

        ipaddress.ip_network(route)
        self.route = route
        self.port_ranges = [
            _coerce_port_range(port_range)
            for port_range in (port_ranges if port_ranges is not None else ports or [])
        ]

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"route": self.route}
        if self.port_ranges:
            body["portRanges"] = [
                port_range.to_dict() for port_range in self.port_ranges
            ]
        return body


@dataclass
class NetworkPolicy:
    """Network egress allowlist for a container.

    A missing policy leaves networking unrestricted for backwards compatibility.
    A present policy allows only its ``allowed_routes``.
    """

    allowed_routes: list[AllowedRoute] = field(default_factory=list)
    """Allowed destination routes and their permitted destination ports."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowedRoutes": [
                allowed_route.to_dict() for allowed_route in self.allowed_routes
            ]
        }


@dataclass
class ContainerInfo:
    """Metadata about a running container."""

    id: str
    """Opaque container identifier assigned by the service."""

    name: str
    """DNS-safe container name."""

    status: str
    """Lifecycle state reported by the service (e.g. ``"Running"``)."""

    pod_name: str
    """Underlying Kubernetes pod name."""

    web_url: str | None = None
    """Optional URL exposing the container's port, if one is published."""


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------


class Container:
    """A managed container backed by the Chalk ``ContainerService``.

    Orchestrates image building, local file upload via volumes, and
    container lifecycle in a single high-level API.

    Examples
    --------
    Build an image, run a container, and exec a command inside it:

    >>> from chalkcompute import Container, Image
    >>> c = Container(
    ...     image=(
    ...         Image.debian_slim()
    ...         .pip_install(["requests"])
    ...         .add_local_file("./script.py", "/app/script.py")
    ...     ),
    ...     cpu="1",
    ...     memory="2Gi",
    ...     env={"LOG_LEVEL": "INFO"},
    ... ).run()
    >>> result = c.exec("python", "/app/script.py")
    >>> result.stdout_text
    'hello\\n'
    >>> c.stop()
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
        secrets: list[Secret] | None = None,
        volumes: list[tuple[str, str]] | None = None,
        port: int | None = None,
        entrypoint: list[str] | None = None,
        lifetime: str | None = None,
        enable_ssh: bool = False,
        security_policy: ContainerSecurityPolicy | None = None,
        network_policy: NetworkPolicy | None = None,
        tags: dict[str, str] | None = None,
        chalk_client: Any | None = None,
    ) -> None:
        self._image = image
        if name is not None:
            validate_dns_label(name)
        self._name = name
        if cpu is not None and not isinstance(cpu, str):
            raise TypeError(f"cpu must be a string such as '2' or '500m', got {cpu!r}")
        if memory is not None and not isinstance(memory, str):
            raise TypeError(
                f"memory must be a string such as '2Gi' or '512Mi', got {memory!r}"
            )
        if gpu is not None and not isinstance(gpu, str):
            raise TypeError(f"gpu must be a string such as 'nvidia-l4', got {gpu!r}")
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        validate_env(env)
        self._env = dict(env) if env else {}
        self._secrets = list(secrets or [])
        referenced_volumes: list[tuple[str, str]] = []
        if volumes is not None:
            for item in volumes:
                if not (
                    isinstance(item, (tuple, list))
                    and len(item) == 2
                    and all(isinstance(s, str) for s in item)
                ):
                    raise ValueError(
                        f"Each volumes entry must be a (name, mount_path) string tuple, got {item!r}. "
                        f"Example: volumes=[('my-volume', '/data')]"
                    )
                # User-referenced volumes carry no type of their own; their
                # backend (hence mount type) is resolved at run() time via
                # resolve_referenced_volume_type, not defaulted here.
                referenced_volumes.append((item[0], item[1]))
        self._referenced_volumes: list[tuple[str, str]] = referenced_volumes
        # Populated at run() time: referenced volumes (typed by lookup) plus
        # any freshly uploaded volumes (typed by the upload driver).
        self._volumes: list[VolumeMount] = []
        if port is not None and not (1 <= port <= 65535):
            raise ValueError(f"port must be between 1 and 65535, got {port!r}")
        self._volume_driver: VolumeDriver | None = None
        self._port = port
        self._entrypoint = entrypoint
        if lifetime is not None:
            from google.protobuf import duration_pb2
            from google.protobuf.json_format import Parse, ParseError

            try:
                _parsed_lifetime = Parse(f'"{lifetime}"', duration_pb2.Duration())  # ty: ignore[unresolved-attribute]
            except ParseError:
                raise ValueError(
                    f"Invalid lifetime {lifetime!r}. "
                    "Use a protobuf duration string (e.g. '30s', '1.5s', '300s')."
                )
            if _parsed_lifetime.seconds == 0 and _parsed_lifetime.nanos == 0:
                raise ValueError(
                    f"Invalid lifetime {lifetime!r}: lifetime must be greater than 0 "
                    "(e.g. '30s', '5m')."
                )
        self._lifetime = lifetime
        self._enable_ssh = enable_ssh
        self._security_policy = security_policy
        self._network_policy = network_policy
        self._tags: dict[str, str] = dict(tags or {})

        # Set after .run()
        self._built_image_uri: str | None = None
        self._container_id: str | None = None
        self._container_info: ContainerInfo | None = None
        self._upserted_secret_names: list[str] = []

        self._client = ConnectClient(chalk_client=chalk_client)

    # -- Alternate constructors ------------------------------------------------

    @classmethod
    def from_id(
        cls, container_id: str, *, chalk_client: Any | None = None
    ) -> Container:
        """Attach to an existing container by ID.

        Parameters
        ----------
        container_id
            Opaque container identifier assigned by the service.

        Returns
        -------
        Container
            A ``Container`` handle bound to the existing container.

        Raises
        ------
        ContainerError
            If the container cannot be found.
        """
        c = cls._make_empty(chalk_client=chalk_client)
        resp = c._client.rpc(_SVC, "GetContainer", {"id": container_id})
        if resp.status_code != 200:
            raise ContainerError(f"GetContainer failed: {resp.text}")
        c._container_info = _parse_container_response(resp.json()["container"])
        c._container_id = c._container_info.id
        return c

    @classmethod
    def from_name(cls, name: str, *, chalk_client: Any | None = None) -> Container:
        """Attach to an existing container by name.

        Parameters
        ----------
        name
            DNS-safe container name.

        Returns
        -------
        Container
            A ``Container`` handle bound to the existing container.

        Raises
        ------
        ContainerError
            If the container cannot be found.
        """
        c = cls._make_empty(chalk_client=chalk_client)
        resp = c._client.rpc(_SVC, "GetContainer", {"name": name})
        if resp.status_code != 200:
            raise ContainerError(f"GetContainer failed: {resp.text}")
        c._container_info = _parse_container_response(resp.json()["container"])
        c._container_id = c._container_info.id
        c._name = name
        return c

    @classmethod
    def list_all(cls, *, chalk_client: Any | None = None) -> list[ContainerInfo]:
        """List all running containers in the current environment.

        Returns
        -------
        list[ContainerInfo]
            Info records for all active containers, ordered by creation time descending.

        Raises
        ------
        ContainerError
            If the request fails.
        """
        c = cls._make_empty(chalk_client=chalk_client)
        resp = c._client.rpc(_SVC, "ListContainers", {})
        if resp.status_code != 200:
            raise ContainerError(f"ListContainers failed: {resp.text}")
        return [
            _parse_container_response(item)
            for item in resp.json().get("containers", [])
        ]

    @classmethod
    def _make_empty(cls, *, chalk_client: Any | None = None) -> Container:
        c = cls.__new__(cls)
        c._image = None  # type: ignore[assignment]
        c._name = None
        c._cpu = None
        c._memory = None
        c._gpu = None
        c._env = {}
        c._secrets = []
        c._referenced_volumes = []
        c._volumes = []
        c._volume_driver = None
        c._port = None
        c._entrypoint = None
        c._lifetime = None
        c._enable_ssh = False
        c._security_policy = None
        c._network_policy = None
        c._tags = {}
        c._built_image_uri = None
        c._container_id = None
        c._container_info = None
        c._upserted_secret_names = []
        c._client = ConnectClient(chalk_client=chalk_client)
        return c

    # -- Image building (Container uses BuildCustomImage with retries) ---------

    def _build_image(
        self,
        *,
        poll_interval: float = 0.5,
        timeout: float = 600.0,
    ) -> str:
        """Build the ``Image`` spec via ``CustomImageService`` and return the URI.

        Parameters
        ----------
        poll_interval
            Seconds between build-status polls.
        timeout
            Maximum seconds to wait for the build to complete.

        Returns
        -------
        str
            The built image URI.

        Raises
        ------
        ImageBuildError
            If the build fails or times out.
        """
        from chalkcompute._connect import ConnectError, build_image
        from chalkcompute._image import Image

        assert isinstance(self._image, Image)
        try:
            return build_image(
                self._client,
                self._image,
                poll_interval=poll_interval,
                timeout=timeout,
            )
        except ConnectError as e:
            raise ImageBuildError(str(e)) from e

    # -- Volume helpers --------------------------------------------------------

    def mount_volume(self, name: str, mount_path: str) -> Container:
        """Mount a named volume into the container.

        Must be called before :meth:`run`. Returns ``self`` for chaining.

        Parameters
        ----------
        name
            Name of the Chalk volume to mount.
        mount_path
            Absolute path inside the container where the volume is mounted.

        Returns
        -------
        Container
            ``self``, to allow chaining.

        Examples
        --------
        >>> from chalkcompute import Container, Image
        >>> c = (
        ...     Container(image=Image.debian_slim())
        ...     .mount_volume("my-data", "/data")
        ...     .run()
        ... )
        """
        # Referenced volume: its mount type is resolved from the backend at
        # run() time, not defaulted here.
        self._referenced_volumes.append((name, mount_path))
        return self

    # -- Lifecycle -------------------------------------------------------------

    @_instrumented("chalkcompute.container.run")
    def run(
        self,
        *,
        poll_interval: float = 0.5,
        build_timeout: float = 600.0,
        ready_timeout: float = 300.0,
    ) -> Container:
        """Build the image, upload local files, and start the container.

        Walks through three phases and blocks until the container is ready
        (or raises): (1) build the image via ``CustomImageService``, (2) if
        the ``Image`` has ``add_local_file`` / ``add_local_dir`` entries
        registered with ``strategy="volume"``, upload them to a managed
        volume and attach it, (3) start the container and poll until it
        reaches the ``Running`` state.

        Parameters
        ----------
        poll_interval
            Seconds between status polls for both build and startup.
        build_timeout
            Maximum seconds to wait for the image build to complete.
        ready_timeout
            Maximum seconds to wait for the container to reach ``Running``.

        Returns
        -------
        Container
            ``self``, to allow chaining.

        Raises
        ------
        ContainerError
            If the container fails to start or times out.
        ImageBuildError
            If the image build fails.

        Examples
        --------
        Build an image from scratch, start a container, inspect it, and
        tear it down:

        >>> from chalkcompute import Container, Image
        >>> img = (
        ...     Image.debian_slim()
        ...     .pip_install(["requests"])
        ...     .add_local_file(
        ...         "./app.py",
        ...         "/app/app.py",
        ...     )
        ...     .workdir("/app")
        ... )
        >>> c = Container(
        ...     image=img,
        ...     cpu="1",
        ...     memory="2Gi",
        ...     env={"LOG_LEVEL": "INFO"},
        ... )
        >>> c.run()
        >>> c.info.status
        'Running'
        >>> c.stop()
        """
        if self._container_id is not None:
            raise ContainerError(
                "Container is already running. Call .stop() before calling .run() again."
            )

        from chalkcompute._image import Image
        from chalkcompute._progress import (
            progress_add,
            progress_done,
            progress_fail,
            progress_finish,
        )

        container_name = sanitize_dns_label(
            self._name or f"container-{uuid.uuid4().hex[:8]}"
        )
        self._name = container_name
        ckey = f"container:{container_name}"
        progress_add(ckey, "container", f'Container "{container_name}"')

        try:
            # 1. Resolve image URI
            if isinstance(self._image, Image):
                ikey = f"image:{container_name}"
                progress_add(ikey, "image", "Building image", parent=ckey)
                logger.info("Building image from %r...", self._image)
                self._built_image_uri = self._build_image(
                    poll_interval=poll_interval,
                    timeout=build_timeout,
                )
                logger.info("Image built: %s", self._built_image_uri)
                progress_done(
                    ikey, detail="cached" if self._built_image_uri else "built"
                )
            else:
                self._built_image_uri = self._image

            # 2a. Resolve each user-referenced volume's real mount type from
            # its backend instead of defaulting; a missing volume fails run().
            self._volumes = [
                VolumeMount(
                    name=name,
                    mount_path=mount_path,
                    type=resolve_referenced_volume_type(self._client, name),
                )
                for name, mount_path in self._referenced_volumes
            ]

            # 2b. Upload lazy local files to a volume (if any)
            lazy_files = (
                self._image.lazy_local_files
                if isinstance(self._image, Image)
                else set()
            )
            if lazy_files:
                vkey = f"volume:{container_name}"
                progress_add(
                    vkey,
                    "volume",
                    f"Uploading {len(lazy_files)} file(s)",
                    parent=ckey,
                )
                logger.info(
                    "Uploading %d local file(s) to volume(s)...", len(lazy_files)
                )
                assert isinstance(self._image, Image)
                driver = _resolve_driver(self._client)
                self._volume_driver = driver
                self._volumes += driver.upload_lazy_files(
                    self._image,
                    volume_prefix="container-files",
                )
                for v in self._volumes:
                    logger.info(
                        "  Volume created: %s (mount: %s)", v.name, v.mount_path
                    )
                progress_done(vkey)

            # 3. Resolve lazy secrets (from_local_env / from_local_env_file)
            if any(s.is_lazy for s in self._secrets):
                skey = f"secret:{container_name}"
                progress_add(skey, "secret", "Resolving secrets", parent=ckey)
                from chalkcompute._secret import resolve_lazy_secrets

                resolved, upserted = resolve_lazy_secrets(self._secrets, self._client)
                self._secrets = resolved
                self._upserted_secret_names = upserted
                logger.info(
                    "Upserted %d secret(s) from local environment.", len(upserted)
                )
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
            if self._security_policy is not None:
                spec["securityPolicy"] = self._security_policy.to_dict()
            if self._network_policy is not None:
                spec["networkPolicy"] = self._network_policy.to_dict()
            if self._tags:
                spec["tags"] = dict(self._tags)

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
                for v in self._volumes:
                    if not v.type:
                        raise VolumeError(
                            f"Volume {v.name!r} mounted at {v.mount_path!r} has no "
                            "mount type; refusing to guess one for the container spec"
                        )
                spec["volumes"] = [v.to_spec_dict() for v in self._volumes]

            if self._secrets:
                spec["secretRefs"] = [s._to_proto_dict() for s in self._secrets]

            logger.info("Starting container '%s'...", container_name)
            resp = self._client.rpc(
                _SVC,
                "RunContainer",
                {"container": {"spec": spec}},
                timeout=60,
            )
            if resp.status_code != 200:
                raise ContainerError(f"RunContainer failed: {resp.text}")

            data = resp.json()
            self._container_info = _parse_container_response(data["container"])
            self._container_id = self._container_info.id
            logger.info(
                "Container created: %s (status=%s)",
                self._container_id,
                self._container_info.status,
            )

            # 5. Poll until Running
            if self._container_info.status != "Running":
                from chalkcompute._progress import progress_update

                progress_update(ckey, detail="booting")
                self._poll_until_running(
                    poll_interval=poll_interval,
                    timeout=ready_timeout,
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
        self,
        *,
        poll_interval: float,
        timeout: float,
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
                if info.status == "Succeeded" and self._lifetime is not None:
                    raise ContainerError(
                        f"Container {self._container_id} expired before becoming ready "
                        f"(lifetime={self._lifetime!r}). The configured lifetime may be "
                        f"shorter than pod scheduling time."
                    )
                raise ContainerError(
                    f"Container {self._container_id} entered {info.status} state"
                )
            time.sleep(poll_interval)
        status = self._container_info.status if self._container_info else "unknown"
        raise ContainerError(
            f"Container {self._container_id} timed out after {timeout}s "
            f"(last status: {status})"
        )

    def _wait_for_volume_files(
        self,
        *,
        timeout: float = 120.0,
        poll_interval: float = 0.5,
    ) -> None:
        """Poll until at least one uploaded file is visible in the ChalkFS mount.

        Parameters
        ----------
        timeout
            Maximum seconds to wait before giving up.
        poll_interval
            Seconds between probes.

        Raises
        ------
        ContainerError
            If no uploaded file becomes visible before the timeout.
        """
        from chalkcompute._image import Image

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

    @_instrumented("chalkcompute.container.exec")
    def exec(
        self,
        *command: str,
        timeout_secs: int | None = None,
        _retries: int = 12,
        _retry_delay: float = 5.0,
    ) -> ExecResult:
        """Execute a command in the running container.

        The command runs as a one-shot process inside the already-started
        container (equivalent to ``docker exec``).  Output is captured in
        full and returned at once; use :meth:`Sandbox.exec` if you need a
        streaming interface.

        Parameters
        ----------
        *command
            Command and arguments to execute (e.g. ``"cat", "/app/app.py"``).
        timeout_secs
            Optional execution timeout in seconds.

        Returns
        -------
        ExecResult
            The stdout, stderr, and exit code of the executed command.

        Raises
        ------
        ContainerError
            If the container is not running or the exec fails.

        Examples
        --------
        Build an image, start a container, run a few commands against it,
        and clean up:

        >>> from chalkcompute import Container, Image
        >>> c = Container(
        ...     image=(
        ...         Image.debian_slim()
        ...         .pip_install(["requests"])
        ...         .add_local_file(
        ...             "./fetch.py",
        ...             "/app/fetch.py",
        ...         )
        ...     ),
        ...     cpu="1",
        ...     memory="1Gi",
        ... ).run()
        >>> hello = c.exec(
        ...     "python",
        ...     "/app/fetch.py",
        ...     "https://example.com",
        ... )
        >>> hello.exit_code
        0
        >>> hello.stdout_text.splitlines()[0]
        '<!doctype html>'
        >>> err = c.exec("ls", "/does-not-exist")
        >>> err.exit_code
        2
        >>> err.stderr_text
        "ls: cannot access '/does-not-exist': No such file or directory\\n"
        >>> c.stop()
        """
        if self._container_id is None:
            raise ContainerError("Container is not running. Call .run() first.")

        body: dict[str, Any] = {
            "id": self._container_id,
            "command": list(command),
        }
        if timeout_secs is not None:
            body["timeout"] = f"{timeout_secs}s"

        # Add buffer so the HTTP connection outlives the in-container exec
        # timeout rather than racing it.
        rpc_timeout = (timeout_secs + 10) if timeout_secs is not None else 30

        # TODO(CHA-8630): Remove retry loop once the API reports accurate
        # Running status (container may not be exec-ready immediately).
        for attempt in range(_retries + 1):
            resp = self._client.rpc(_SVC, "ExecCommand", body, timeout=rpc_timeout)
            if resp.status_code == 200:
                data = resp.json()
                stdout_b64 = data.get("stdout", "")
                stderr_b64 = data.get("stderr", "")
                return ExecResult(
                    stdout=base64.b64decode(stdout_b64) if stdout_b64 else b"",
                    stderr=base64.b64decode(stderr_b64) if stderr_b64 else b"",
                    exit_code=data.get("exitCode", 0),
                )
            if "container not found" not in resp.text or attempt >= _retries:
                raise ContainerError(f"ExecCommand failed: {resp.text}")
            time.sleep(_retry_delay)

        raise ContainerError(f"ExecCommand failed after {_retries} retries")

    @_instrumented("chalkcompute.container.stop")
    def stop(self, grace_period_seconds: int | None = None) -> None:
        """Stop the container and clean up temporary volumes and secrets.

        Parameters
        ----------
        grace_period_seconds
            Optional grace period before forceful termination.
        """
        if self._container_id is None:
            raise ContainerError("container has not been started; call .run() first")
        if self._container_id is not None:
            body: dict[str, Any] = {"id": self._container_id}
            if grace_period_seconds is not None:
                body["gracePeriodSeconds"] = grace_period_seconds
            self._client.rpc(_SVC, "StopContainer", body)
            self._container_id = None
            self._container_info = None

        if self._volumes:
            # Driver is used here only to perform the delete action, not to
            # derive a mount type.
            driver = self._volume_driver or _resolve_driver(self._client)
            cleanup_owned_volumes(driver, self._volumes, self._referenced_volumes)
        self._volumes = []
        self._volume_driver = None

        if self._upserted_secret_names:
            from chalkcompute._secret import delete_secrets

            delete_secrets(self._client, self._upserted_secret_names)
            self._upserted_secret_names = []

    def refresh(self) -> ContainerInfo:
        """Fetch latest container status from the server.

        Returns
        -------
        ContainerInfo
            The latest metadata for the container.

        Raises
        ------
        ContainerError
            If no container ID is set (``run()`` was never called).
        """
        if self._container_id is None:
            raise ContainerError("No container ID — call .run() first.")
        resp = self._client.rpc(_SVC, "GetContainer", {"id": self._container_id})
        if resp.status_code != 200:
            raise ContainerError(f"GetContainer failed: {resp.text}")
        self._container_info = _parse_container_response(resp.json()["container"])
        return self._container_info

    @property
    def id(self) -> str | None:
        """Opaque container identifier assigned by the service.

        ``None`` until :meth:`run` (or :meth:`from_id` / :meth:`from_name`)
        has resolved the container against the service.
        """
        return self._container_id

    @property
    def info(self) -> ContainerInfo | None:
        """The most recently fetched :class:`ContainerInfo` snapshot.

        ``None`` until the container has been started or attached to.
        Populated by :meth:`run`, :meth:`from_id`, and :meth:`from_name`.
        Call :meth:`refresh_info` to fetch the latest state from the service.
        """
        return self._container_info

    @property
    def image_uri(self) -> str | None:
        """The fully qualified registry URI of the image the container uses.

        Set after :meth:`run` finishes the image build (or, if the container
        was constructed with a string image reference, equal to that
        reference).  ``None`` before the container has been started.
        """
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


def _coerce_port_range(port_range: PortRangeInput) -> PortRange:
    if isinstance(port_range, PortRange):
        return port_range
    if isinstance(port_range, int) and not isinstance(port_range, bool):
        return PortRange(port_range)
    if isinstance(port_range, tuple) and len(port_range) == 2:
        start_port, end_port = port_range
        return PortRange(start_port, end_port)
    raise TypeError("port ranges must be PortRange, int, or (start_port, end_port)")


def _validate_port_range(start_port: int, end_port: int) -> None:
    _validate_port(start_port, "start_port")
    _validate_port(end_port, "end_port")
    if end_port < start_port:
        raise ValueError("end_port must be greater than or equal to start_port")


def _validate_port(port: int, name: str) -> None:
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError(f"{name} must be an integer")
    if port < 1 or port > 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
