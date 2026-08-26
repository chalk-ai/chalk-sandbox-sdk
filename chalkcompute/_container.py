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

import logging
import queue
import time
import uuid
import warnings
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, NoReturn

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from connectrpc.protocol import ProtocolType
from protobuf import Oneof
from protobuf.wkt import Duration

from chalkcompute._connect import (
    ConnectClient,
    get_connect_client,
    raise_for_rpc_error,
    rpc_address_from_target,
    rpc_error_message,
)
from chalkcompute._containerspec import (
    ComputeClass,
    ContainerSecurityPolicy,
    ContainerSpec,
    KernelPolicy as KernelPolicy,
    NetworkPolicy,
)
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.container.v1 import (
    service_connect as container_connect,
)
from chalkcompute._gen.chalk.sandbox.v2 import process_service_pb as pb
from chalkcompute._gen.chalk.sandbox.v2 import (
    process_service_connect as pb_connect,
)
from chalkcompute._http import SYNC_HTTP_CLIENT
from chalkcompute._naming import (
    sanitize_dns_label,
    validate_env,
)
from chalkcompute._rpc import RPC_READ_MAX_BYTES, rpc_interceptors
from chalkcompute._tracing import _instrumented
from chalkcompute._volume import (
    Volume,
    VolumeClient,
    VolumeMount,
    cleanup_owned_volumes,
    normalize_volume_refs,
    referenced_volume_mount,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from chalkcompute._image import Image as _Image
    from chalkcompute._secret import Secret

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


@dataclass
class ContainerSession:
    """Interactive handle to a persistent command session in a container."""

    @dataclass
    class Status:
        """Current process state for a persistent container session."""

        class State(Enum):
            UNSPECIFIED = "unspecified"
            RUNNING = "running"
            EXITED = "exited"
            FAILED = "failed"
            TIMED_OUT = "timed_out"

        state: State
        """Process lifecycle state."""

        exit_code: int | None = None
        """Exit code when the process exited or timed out with one."""

        signal: int | None = None
        """Signal number when the process was terminated by a signal."""

    @dataclass
    class Info:
        """Metadata about a persistent command session in a container."""

        id: str
        """Opaque session identifier assigned by the service."""

        container_id: str
        """Container that owns this session."""

        command: str
        """Command binary or script used to start the session."""

        args: list[str]
        """Arguments passed to ``command``."""

        workdir: str | None
        """Working directory used for the process, if one was provided."""

        env: dict[str, str]
        """Environment overrides used for the process."""

        timeout_secs: int | None
        """Process timeout in seconds, if one was provided."""

        pty: bool
        """Whether the session was started or attached with a pty."""

        status: ContainerSession.Status
        """Latest process status reported by the service."""

    class Event:
        """Base class for events from a persistent container session stream."""

    @dataclass(frozen=True)
    class UnknownEvent(Event):
        """Unknown event kind returned by the service."""

    @dataclass(frozen=True)
    class AttachedEvent(Event):
        """The stream attached to a session."""

        session_id: str

    @dataclass(frozen=True)
    class OutputEvent(Event):
        """Output emitted by the session process."""

        class Stream(Enum):
            STDOUT = "stdout"
            STDERR = "stderr"
            PTY_OUTPUT = "pty_output"

        stream: Stream
        data: bytes

        @property
        def text(self) -> str:
            return self.data.decode("utf-8", errors="replace")

    @dataclass(frozen=True)
    class StatusUpdateEvent(Event):
        """The latest process status."""

        status: ContainerSession.Status

    @dataclass(frozen=True)
    class ExitedEvent(Event):
        """The session process exited."""

        status: ContainerSession.Status

    @dataclass(frozen=True)
    class DetachedEvent(Event):
        """This stream detached from the session."""

    @dataclass(frozen=True)
    class FailedEvent(Event):
        """The session process failed before producing an exit status."""

        message: str

    @dataclass(frozen=True)
    class TimedOutEvent(Event):
        """The session process timed out."""

        status: ContainerSession.Status

    @dataclass(frozen=True)
    class ErrorEvent(Event):
        """The session stream returned an API error."""

        code: str
        message: str | None = None

        @property
        def error(self) -> str:
            if self.message:
                return f"{self.code}: {self.message}"
            return self.code

    @dataclass
    class Result:
        """Result returned after a session process finishes."""

        session_id: str
        exit_code: int | None = None
        signal: int | None = None
        timed_out: bool = False
        failed: bool = False
        failure_message: str | None = None
        error: str | None = None

        @property
        def ok(self) -> bool:
            return (
                self.exit_code == 0
                and not self.timed_out
                and not self.failed
                and self.error is None
            )

    _client: ContainerSessionClient = field(repr=False)
    _container_id: str = field(repr=False)
    _request_queue: queue.Queue[pb.OpenSessionRequest | None] = field(repr=False)
    _response_iterator: Iterator[pb.OpenSessionResponse] = field(repr=False)
    _pending_events: deque[ContainerSession.Event] = field(
        default_factory=deque,
        repr=False,
    )
    _id: str = ""
    _closed_requests: bool = field(default=False, repr=False)
    _detached: bool = field(default=False, repr=False)

    @property
    def id(self) -> str:
        """Opaque session identifier assigned by the service."""
        return self._id

    def write(self, data: str | bytes) -> None:
        """Write data to the session process's stdin."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._send(
            pb.OpenSessionRequest(command=Oneof("stdin_data", pb.StdinData(data=data)))
        )

    def close_stdin(self) -> None:
        """Signal EOF on stdin."""
        self._send(pb.OpenSessionRequest(command=Oneof("stdin_eof", pb.StdinEof())))

    def signal(self, signum: int) -> None:
        """Send a signal to the session process."""
        self._send(
            pb.OpenSessionRequest(
                command=Oneof("signal", pb.SessionSignal(signal=signum))
            )
        )

    def resize(self, cols: int, rows: int) -> None:
        """Resize the session pty."""
        self._send(
            pb.OpenSessionRequest(
                command=Oneof("resize_pty", _make_resize_pty(cols, rows))
            )
        )

    def status(self) -> ContainerSession.Status:
        """Fetch the latest process status for this session."""
        return self._client.get_session(self._container_id, self._id).status

    def detach(self) -> None:
        """Detach this stream from the session without stopping the process."""
        if self._detached:
            return
        self._send(
            pb.OpenSessionRequest(command=Oneof("detach_session", pb.DetachSession()))
        )
        for event in self.events():
            match event:
                case ContainerSession.DetachedEvent():
                    self._detached = True
                    self._close_request_stream()
                    return
                case ContainerSession.ErrorEvent() as error:
                    self._close_request_stream()
                    raise ContainerError(f"Session detach failed: {error.error}")
                case (
                    ContainerSession.ExitedEvent()
                    | ContainerSession.FailedEvent()
                    | ContainerSession.TimedOutEvent()
                ):
                    self._close_request_stream()
                    return
        self._detached = True
        self._close_request_stream()

    def events(self) -> Iterator[ContainerSession.Event]:
        """Iterate over events from this session stream."""
        while self._pending_events:
            event = self._pending_events.popleft()
            yield event
            match event:
                case ContainerSession.ErrorEvent():
                    return
                case (
                    ContainerSession.DetachedEvent()
                    | ContainerSession.ExitedEvent()
                    | ContainerSession.FailedEvent()
                    | ContainerSession.TimedOutEvent()
                ):
                    self._close_request_stream()
                    return

        try:
            for response in self._response_iterator:
                event = _pb_to_session_event(response)
                match event:
                    case ContainerSession.AttachedEvent(session_id=session_id):
                        self._id = session_id
                yield event
                match event:
                    case ContainerSession.ErrorEvent():
                        return
                    case (
                        ContainerSession.DetachedEvent()
                        | ContainerSession.ExitedEvent()
                        | ContainerSession.FailedEvent()
                        | ContainerSession.TimedOutEvent()
                    ):
                        self._close_request_stream()
                        return
        except ConnectRpcError as e:
            _handle_container_rpc_error(e, self._container_id, self._id)

    def wait(self) -> ContainerSession.Result:
        """Wait for the session process to finish."""
        exit_code: int | None = None
        signal: int | None = None
        timed_out = False
        failed = False
        failure_message: str | None = None
        error_message: str | None = None

        for event in self.events():
            match event:
                case ContainerSession.ExitedEvent(status=status):
                    exit_code = status.exit_code
                    signal = status.signal
                    break
                case ContainerSession.TimedOutEvent(status=status):
                    exit_code = status.exit_code
                    signal = status.signal
                    timed_out = True
                    break
                case ContainerSession.FailedEvent(message=message):
                    failed = True
                    failure_message = message
                    break
                case ContainerSession.ErrorEvent() as error:
                    error_message = error.error
                    break

        self._close_request_stream()
        return ContainerSession.Result(
            session_id=self._id,
            exit_code=exit_code,
            signal=signal,
            timed_out=timed_out,
            failed=failed,
            failure_message=failure_message,
            error=error_message,
        )

    def _await_attached(self) -> None:
        for response in self._response_iterator:
            event = _pb_to_session_event(response)
            match event:
                case ContainerSession.AttachedEvent(session_id=session_id):
                    self._id = session_id
                    return
                case ContainerSession.ErrorEvent() as error:
                    self._close_request_stream()
                    raise ContainerError(f"Session failed: {error.error}")
                case _:
                    self._pending_events.append(event)
        self._close_request_stream()
        raise ContainerError("Session stream ended before the session was attached")

    def _send(self, request: pb.OpenSessionRequest) -> None:
        if self._closed_requests:
            raise ContainerError("Session stream is closed")
        self._request_queue.put(request)

    def _close_request_stream(self) -> None:
        if not self._closed_requests:
            self._request_queue.put(None)
            self._closed_requests = True


class ContainerSessionClient:
    """Internal ConnectRPC client for container sessions."""

    def __init__(
        self,
        target: str,
        *,
        credentials: object | None,
        metadata: list[tuple[str, str]],
        _connect: ConnectClient | None = None,
    ) -> None:
        self._target = target
        self._static_metadata = metadata
        self._connect = _connect
        self._stub = pb_connect.SandboxProcessServiceClientSync(
            rpc_address_from_target(target, use_tls=credentials is not None),
            protocol=ProtocolType.GRPC,
            interceptors=rpc_interceptors(),
            http_client=SYNC_HTTP_CLIENT,
            read_max_bytes=RPC_READ_MAX_BYTES,
        )

    @classmethod
    def from_connect(cls, connect: ConnectClient) -> ContainerSessionClient:
        connect._ensure_auth()
        target = connect._api_server or ""
        return cls(
            target,
            credentials=None,
            metadata=connect._rpc_metadata(),
            _connect=connect,
        )

    @property
    def _metadata(self) -> list[tuple[str, str]]:
        if self._connect is not None:
            return self._connect._rpc_metadata()
        return self._static_metadata

    @_metadata.setter
    def _metadata(self, value: list[tuple[str, str]]) -> None:
        self._static_metadata = value

    def close(self) -> None:
        self._stub.close()

    def new_session(
        self,
        container_id: str,
        command: str,
        *args: str,
        timeout_secs: int | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        pty: bool | tuple[int, int] = False,
    ) -> ContainerSession:
        validate_env(env)
        process = pb.NewProcess(
            sandbox_id=container_id,
            command=command,
            args=list(args),
        )
        if workdir is not None:
            process.workdir = workdir
        if env is not None:
            for key, value in env.items():
                process.env_vars[key] = value
        if timeout_secs is not None:
            process.timeout = Duration(seconds=timeout_secs)
        pty_info = _coerce_pty_info(pty)
        if pty_info is not None:
            process.pty_info = pty_info

        return self._start_session(
            container_id,
            pb.OpenSessionRequest(command=Oneof("new_process", process)),
        )

    def attach_session(
        self,
        container_id: str,
        session_id: str,
        *,
        pty: bool | tuple[int, int] = False,
    ) -> ContainerSession:
        attach = pb.AttachSession(sandbox_id=container_id, session_id=session_id)
        pty_info = _coerce_pty_info(pty)
        if pty_info is not None:
            attach.pty_info = pty_info
        return self._start_session(
            container_id,
            pb.OpenSessionRequest(command=Oneof("attach_session", attach)),
        )

    def get_session(
        self,
        container_id: str,
        session_id: str,
    ) -> ContainerSession.Info:
        try:
            resp = self._stub.get_session(
                pb.GetSessionRequest(sandbox_id=container_id, session_id=session_id),
                headers=dict(self._metadata),
            )
        except ConnectRpcError as e:
            _handle_container_rpc_error(e, container_id, session_id)
        if resp.session is None:
            raise ContainerError(f"GetSession returned no session: {session_id}")
        return _pb_to_session_info(container_id, resp.session)

    def list_sessions(self, container_id: str) -> list[ContainerSession.Info]:
        try:
            resp = self._stub.list_sessions(
                pb.ListSessionsRequest(sandbox_id=container_id),
                headers=dict(self._metadata),
            )
        except ConnectRpcError as e:
            _handle_container_rpc_error(e, container_id)
        return [_pb_to_session_info(container_id, session) for session in resp.sessions]

    def exec_command(
        self,
        container_id: str,
        command: str,
        *args: str,
        timeout_secs: int | None = None,
    ) -> pb.ExecCommandResponse:
        request = pb.ExecCommandRequest(
            sandbox_id=container_id,
            command=command,
            args=list(args),
        )
        if timeout_secs is not None:
            request.timeout = Duration(seconds=timeout_secs)
        try:
            return self._stub.exec_command(request, headers=dict(self._metadata))
        except ConnectRpcError as e:
            _handle_container_rpc_error(e, container_id)
            raise

    def _start_session(
        self,
        container_id: str,
        initial_request: pb.OpenSessionRequest,
    ) -> ContainerSession:
        request_queue: queue.Queue[pb.OpenSessionRequest | None] = queue.Queue()
        request_queue.put(initial_request)

        def request_iterator() -> Iterator[pb.OpenSessionRequest]:
            while True:
                request = request_queue.get()
                if request is None:
                    return
                yield request

        try:
            response_iterator = self._stub.open_session(
                request_iterator(),
                headers=dict(self._metadata),
            )
        except ConnectRpcError as e:
            _handle_container_rpc_error(e, container_id)

        session = ContainerSession(
            _client=self,
            _container_id=container_id,
            _request_queue=request_queue,
            _response_iterator=response_iterator,
        )
        try:
            session._await_attached()
        except ConnectRpcError as e:
            _handle_container_rpc_error(e, container_id)
        return session


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
        volumes: list[tuple[str, str] | Volume | VolumeMount] | None = None,
        port: int | None = None,
        entrypoint: list[str] | None = None,
        lifetime: str | None = None,
        enable_ssh: bool = False,
        security_policy: ContainerSecurityPolicy | None = None,
        network_policy: NetworkPolicy | None = None,
        compute_class: ComputeClass | None = None,
        tags: dict[str, str] | None = None,
        chalk_client: Any | None = None,
    ) -> None:
        warnings.warn(
            "`Container` is deprecated. Use `Sandbox` instead; it is the "
            "supported API for creating, managing, and executing commands in "
            "sandboxes.",
            DeprecationWarning,
            stacklevel=2,
        )
        if image is None:
            raise TypeError(
                "image must be an Image or image reference string, got None"
            )
        ContainerSpec.validate_fields(
            image=image,
            name=name,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            env=env,
            entrypoint=entrypoint,
            network_policy=network_policy,
        )
        self._image: _Image | str | None = image
        self._name = name
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        self._env = dict(env) if env else {}
        self._secrets = list(secrets or [])
        # Existence is checked at run() time, not here.
        self._referenced_volumes: list[VolumeMount] = normalize_volume_refs(volumes)
        # Populated at run() time: referenced volumes plus any freshly
        # uploaded volumes.
        self._volumes: list[VolumeMount] = []
        if port is not None and not (1 <= port <= 65535):
            raise ValueError(f"port must be between 1 and 65535, got {port!r}")
        self._volume_driver: VolumeClient | None = None
        self._port = port
        self._entrypoint = list(entrypoint) if entrypoint is not None else None
        if lifetime is not None:
            try:
                _parsed_lifetime = Duration.from_json(f'"{lifetime}"')
            except ValueError:
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
        self._compute_class = compute_class
        self._tags: dict[str, str] = dict(tags or {})

        # Set after .run()
        self._built_image_uri: str | None = None
        self._container_id: str | None = None
        self._container_info: ContainerInfo | None = None

        self._client = get_connect_client(chalk_client)
        self._session_client: ContainerSessionClient | None = None

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
        c._container_info = _get_container_info(
            c._client, container_pb.GetContainerRequest(id=container_id)
        )
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
        c._container_info = _get_container_info(
            c._client, container_pb.GetContainerRequest(name=name)
        )
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
        stub = c._client.stub(container_connect.ContainerServiceClientSync)
        try:
            resp = c._client.call(
                stub.list_containers, container_pb.ListContainersRequest()
            )
        except ConnectRpcError as e:
            raise ContainerError(
                f"ListContainers failed: {rpc_error_message(e)}"
            ) from e
        return [_parse_container_response(item) for item in resp.containers]

    @classmethod
    def _make_empty(cls, *, chalk_client: Any | None = None) -> Container:
        c = cls.__new__(cls)
        c._image = None
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
        c._client = get_connect_client(chalk_client)
        c._session_client = None
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
        self._referenced_volumes.append(VolumeMount(name=name, mount_path=mount_path))
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

            # 2a. A missing user-referenced volume fails run().
            self._volumes = [
                referenced_volume_mount(self._client, mount)
                for mount in self._referenced_volumes
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
                    "Uploading %d local file(s) to volume(s)...",
                    len(lazy_files),
                )
                assert isinstance(self._image, Image)
                driver = VolumeClient.from_connect(self._client)
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

            # 3. Resolve lazy secrets (from_local_env / from_local_env_file).
            # Resolved into a local, not self._secrets, so a later .run() on
            # the same object re-upserts instead of reusing a stale resolution.
            resolved_secrets = self._secrets
            if any(s.is_lazy for s in self._secrets):
                skey = f"secret:{container_name}"
                progress_add(skey, "secret", "Resolving secrets", parent=ckey)
                from chalkcompute._secret import SecretScope, resolve_lazy_secrets

                scope = SecretScope.sandbox(container_name)
                resolved_secrets, upserted = resolve_lazy_secrets(
                    self._secrets, self._client, scope=scope
                )
                logger.info(
                    "Upserted %d secret(s) from local environment.",
                    len(upserted),
                )
                progress_done(skey, detail=f"{len(upserted)} secret(s)")

            # 4. Build the ContainerService spec
            assert self._built_image_uri is not None
            spec = ContainerSpec(
                name=container_name,
                image=self._built_image_uri,
                entrypoint=(
                    self._entrypoint
                    if self._entrypoint is not None
                    else ["sleep", "infinity"]
                ),
                cpu=self._cpu,
                memory=self._memory,
                gpu=self._gpu,
                env=self._env,
                volumes=self._volumes,
                secret_refs=resolved_secrets,
                port=self._port,
                lifetime=self._lifetime,
                enable_ssh=self._enable_ssh,
                security_policy=self._security_policy,
                network_policy=self._network_policy,
                compute_class=self._compute_class,
                tags=self._tags,
            ).to_proto()

            logger.info("Starting container '%s'...", container_name)
            request = container_pb.RunContainerRequest(
                container=container_pb.ContainerRequest(spec=spec)
            )
            stub = self._client.stub(container_connect.ContainerServiceClientSync)
            try:
                resp = self._client.call(stub.run_container, request, timeout=60)
            except ConnectRpcError as e:
                raise ContainerError(
                    f"RunContainer failed: {rpc_error_message(e)}"
                ) from e

            if resp.container is None:
                raise ContainerError("RunContainer returned no container")
            self._container_info = _parse_container_response(resp.container)
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
        request = container_pb.GetContainerRequest(id=self._container_id)
        while time.monotonic() < deadline:
            info = _get_container_info(self._client, request)
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
            f"Container {self._container_id} timed out after {timeout}s (last status: {status})"
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

        if not command:
            raise ValueError("exec() requires a command")

        response = self._get_session_client().exec_command(
            self._container_id,
            command[0],
            *command[1:],
            timeout_secs=timeout_secs,
        )
        return ExecResult(
            stdout=response.stdout,
            stderr=response.stderr,
            exit_code=response.exit_code,
        )

    @_instrumented("chalkcompute.container.new_session")
    def new_session(
        self,
        command: str,
        *args: str,
        timeout_secs: int | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        pty: bool | tuple[int, int] = False,
    ) -> ContainerSession:
        """Start a persistent command session in the running container.

        The returned :class:`ContainerSession` can stream output, accept stdin,
        detach, and later be reattached with :meth:`attach_session`.
        """
        container_id = self._require_container_id()
        return self._get_session_client().new_session(
            container_id,
            command,
            *args,
            timeout_secs=timeout_secs,
            workdir=workdir,
            env=env,
            pty=pty,
        )

    @_instrumented("chalkcompute.container.attach_session")
    def attach_session(
        self,
        session_id: str,
        *,
        pty: bool | tuple[int, int] = False,
    ) -> ContainerSession:
        """Attach a new stream to an existing persistent session."""
        container_id = self._require_container_id()
        return self._get_session_client().attach_session(
            container_id,
            session_id,
            pty=pty,
        )

    def get_session(self, session_id: str) -> ContainerSession.Info:
        """Fetch metadata for a persistent session owned by this container."""
        container_id = self._require_container_id()
        return self._get_session_client().get_session(container_id, session_id)

    def list_sessions(self) -> list[ContainerSession.Info]:
        """List persistent sessions owned by this container."""
        container_id = self._require_container_id()
        return self._get_session_client().list_sessions(container_id)

    def _require_container_id(self) -> str:
        if self._container_id is None:
            raise ContainerError("Container is not running. Call .run() first.")
        return self._container_id

    def _get_session_client(self) -> ContainerSessionClient:
        if self._session_client is None:
            self._session_client = ContainerSessionClient.from_connect(self._client)
        return self._session_client

    @_instrumented("chalkcompute.container.stop")
    def stop(self, grace_period_seconds: int | None = None) -> None:
        """Stop the container and clean up temporary volumes.

        Parameters
        ----------
        grace_period_seconds
            Optional grace period before forceful termination.
        """
        if self._container_id is None:
            raise ContainerError("container has not been started; call .run() first")
        if self._container_id is not None:
            request = container_pb.StopContainerRequest(id=self._container_id)
            if grace_period_seconds is not None:
                request.grace_period_seconds = grace_period_seconds
            stub = self._client.stub(container_connect.ContainerServiceClientSync)
            try:
                self._client.call(stub.stop_container, request)
            except ConnectRpcError as e:
                # Log, don't raise: cleanup must proceed.
                logger.warning(
                    "StopContainer for %s failed: %s",
                    self._container_id,
                    rpc_error_message(e),
                )
            self._container_id = None
            self._container_info = None

        if self._session_client is not None:
            self._session_client.close()
            self._session_client = None

        if self._volumes:
            # Driver is used here only to perform the delete action, not to
            # derive a mount type.
            driver = self._volume_driver or VolumeClient.from_connect(self._client)
            cleanup_owned_volumes(driver, self._volumes, self._referenced_volumes)
        self._volumes = []
        self._volume_driver = None

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
        self._container_info = _get_container_info(
            self._client, container_pb.GetContainerRequest(id=self._container_id)
        )
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


def _parse_container_response(
    container: container_pb.ContainerResponse,
) -> ContainerInfo:
    return ContainerInfo(
        id=container.id,
        name=container.name,
        status=container.status,
        pod_name=container.pod_name,
        web_url=container.web_url or None,
    )


def _get_container_info(
    client: ConnectClient,
    request: container_pb.GetContainerRequest,
) -> ContainerInfo:
    """Fetch a container via ``GetContainer`` and parse it, or raise ContainerError."""
    stub = client.stub(container_connect.ContainerServiceClientSync)
    try:
        resp = client.call(stub.get_container, request)
    except ConnectRpcError as e:
        raise ContainerError(f"GetContainer failed: {rpc_error_message(e)}") from e
    if resp.container is None:
        raise ContainerError("GetContainer returned no container")
    return _parse_container_response(resp.container)


def _process_states() -> dict[int, ContainerSession.Status.State]:
    return {
        pb.ProcessState.RUNNING: ContainerSession.Status.State.RUNNING,
        pb.ProcessState.EXITED: ContainerSession.Status.State.EXITED,
        pb.ProcessState.FAILED: ContainerSession.Status.State.FAILED,
        pb.ProcessState.TIMED_OUT: ContainerSession.Status.State.TIMED_OUT,
    }


def _pb_to_session_info(
    container_id: str,
    info: pb.SessionInfo,
) -> ContainerSession.Info:
    new_process = info.new_process
    if new_process is None:
        raise ContainerError(f"Session {info.session_id} has no process metadata")
    if info.process_status is None:
        raise ContainerError(f"Session {info.session_id} has no process status")
    return ContainerSession.Info(
        id=info.session_id,
        container_id=container_id,
        command=new_process.command,
        args=list(new_process.args),
        workdir=new_process.workdir if new_process.has_field("workdir") else None,
        env=dict(new_process.env_vars),
        timeout_secs=(new_process.timeout.seconds if new_process.timeout else None),
        pty=new_process.pty_info is not None,
        status=_pb_to_session_status(info.process_status),
    )


def _pb_to_session_status(status: pb.ProcessStatus) -> ContainerSession.Status:
    return ContainerSession.Status(
        state=_process_states().get(
            status.state,
            ContainerSession.Status.State.UNSPECIFIED,
        ),
        exit_code=status.exit_code if status.has_field("exit_code") else None,
        signal=status.signal if status.has_field("signal") else None,
    )


def _pb_to_session_event(response: pb.OpenSessionResponse) -> ContainerSession.Event:
    match response.event:
        case Oneof(field="session_attached", value=attached):
            return ContainerSession.AttachedEvent(
                session_id=attached.session_id,
            )
        case Oneof(field="output_data", value=output):
            match output.stream:
                case pb.OutputData.Stream.STDERR:
                    return ContainerSession.OutputEvent(
                        stream=ContainerSession.OutputEvent.Stream.STDERR,
                        data=output.data,
                    )
                case pb.OutputData.Stream.PTY_OUTPUT:
                    return ContainerSession.OutputEvent(
                        stream=ContainerSession.OutputEvent.Stream.PTY_OUTPUT,
                        data=output.data,
                    )
            return ContainerSession.OutputEvent(
                stream=ContainerSession.OutputEvent.Stream.STDOUT,
                data=output.data,
            )
        case Oneof(field="process_status", value=status):
            return ContainerSession.StatusUpdateEvent(
                status=_pb_to_session_status(status),
            )
        case Oneof(field="process_exited", value=process_exited):
            status = _ended_process_status(
                ContainerSession.Status.State.EXITED,
                process_exited,
            )
            return ContainerSession.ExitedEvent(status=status)
        case Oneof(field="session_detached"):
            return ContainerSession.DetachedEvent()
        case Oneof(field="process_failed", value=process_failed):
            return ContainerSession.FailedEvent(
                message=process_failed.message,
            )
        case Oneof(field="process_timed_out", value=process_timed_out):
            status = _ended_process_status(
                ContainerSession.Status.State.TIMED_OUT,
                process_timed_out,
            )
            return ContainerSession.TimedOutEvent(status=status)
        case Oneof(field="error", value=error):
            return ContainerSession.ErrorEvent(
                code=error.code,
                message=error.message,
            )
        case _:
            return ContainerSession.UnknownEvent()


def _ended_process_status(
    state: ContainerSession.Status.State,
    process_end: pb.ProcessExited | pb.ProcessTimedOut,
) -> ContainerSession.Status:
    return ContainerSession.Status(
        state=state,
        exit_code=process_end.exit_code if process_end.has_field("exit_code") else None,
        signal=process_end.signal if process_end.has_field("signal") else None,
    )


def _coerce_pty_info(pty: bool | tuple[int, int]) -> pb.PtyInfo | None:
    if pty is False:
        return None
    if pty is True:
        return _make_pty_info(80, 24)
    if isinstance(pty, tuple) and len(pty) == 2:
        cols, rows = pty
        return _make_pty_info(cols, rows)
    raise TypeError("pty must be False, True, or a (cols, rows) tuple")


def _make_pty_info(cols: int, rows: int) -> pb.PtyInfo:
    _validate_pty_dimension(cols, "cols")
    _validate_pty_dimension(rows, "rows")
    return pb.PtyInfo(cols=cols, rows=rows)


def _make_resize_pty(cols: int, rows: int) -> pb.ResizePty:
    _validate_pty_dimension(cols, "cols")
    _validate_pty_dimension(rows, "rows")
    return pb.ResizePty(cols=cols, rows=rows)


def _validate_pty_dimension(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ValueError(f"{name} must be at least 1")


def _handle_container_rpc_error(
    error: ConnectRpcError,
    container_id: str,
    session_id: str | None = None,
) -> NoReturn:
    target = f"session {session_id}" if session_id else f"container {container_id}"
    raise_for_rpc_error(
        error,
        {
            Code.NOT_FOUND: (ContainerError, f"{target} not found"),
            Code.FAILED_PRECONDITION: (
                ContainerError,
                f"{target} is not ready for container sessions",
            ),
            Code.UNIMPLEMENTED: (
                ContainerError,
                "Container sessions are not implemented by this server",
            ),
        },
    )
    raise ContainerError(f"Container session RPC failed: {error.message}") from error
