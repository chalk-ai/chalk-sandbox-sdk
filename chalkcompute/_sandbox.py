"""
Python SDK for the Sandbox RPC service.

Usage:
    from chalkcompute import Sandbox

    # Create a sandbox
    sandbox = Sandbox(image="ubuntu:latest").run()
    print(f"Created sandbox: {sandbox.id}")

    # Execute a command and inspect its output
    result = sandbox.exec("python", "-c", "print('hello')")
    print(result.stdout_text)
    print(f"Exit code: {result.exit_code}")

    # Execute with real-time streaming
    for event in sandbox.exec_stream("python", "-c", "import time; [print(i) or time.sleep(1) for i in range(5)]"):
        if event.stdout:
            print(event.stdout, end="")

    # Terminate when done
    sandbox.terminate()
"""

from __future__ import annotations

import queue
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from connectrpc.protocol import ProtocolType
from protobuf import Oneof
from protobuf.wkt import Duration

from chalkcompute._artifact import ArtifactClient, _artifact_from_proto
from chalkcompute._connect import (
    ConnectError,
    ConnectClient,
    build_image,
    grpc_target_from_url,
    raise_for_rpc_error,
    rpc_address_from_target,
)
from chalkcompute._containerspec import ContainerSpec
from chalkcompute._gen.chalk.arrow.v1 import arrow_pb
from chalkcompute._gen.chalk.sandbox.v1 import runtime_service_pb as runtime_pb
from chalkcompute._gen.chalk.sandbox.v1 import (
    runtime_service_connect as runtime_connect,
)
from chalkcompute._gen.chalk.sandbox.v2 import process_service_pb as process_pb
from chalkcompute._gen.chalk.sandbox.v2 import (
    process_service_connect as process_connect,
)
from chalkcompute._gen.chalk.sandbox.v2 import service_pb as pb
from chalkcompute._gen.chalk.sandbox.v2 import service_connect as pb_connect
from chalkcompute._http import SYNC_HTTP_CLIENT
from chalkcompute._image import Image
from chalkcompute._naming import validate_env
from chalkcompute._secret import SecretScope, resolve_lazy_secrets
from chalkcompute._rpc import RPC_READ_MAX_BYTES, rpc_interceptors
from chalkcompute._tracing import _instrumented
from chalkcompute._volume import (
    VolumeClient,
    cleanup_owned_volumes,
    normalize_volume_refs,
    referenced_volume_mount,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from typing import Any

    from chalkcompute._artifact import Artifact
    from chalkcompute._containerspec import (
        ComputeClass,
        ContainerSecurityPolicy,
        NetworkPolicy,
        RestartPolicy,
        SSHDest,
    )
    from chalkcompute._secret import Secret
    from chalkcompute._volume import Volume, VolumeMount

    # Aliases defined before SandboxClient, which has a ``.list()`` method that
    # would otherwise shadow the builtin ``list`` in class-scope name resolution.
    _GrpcMetadata = list[tuple[str, str]]
    _VolumeList = list[tuple[str | Volume, str] | Volume | VolumeMount]
    _SecretList = list[Secret]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SandboxError(Exception):
    """Base exception for sandbox errors."""

    pass


class SandboxNotFoundError(SandboxError):
    """Raised when a sandbox is not found."""

    pass


class SandboxStatusError(SandboxError):
    """Sandbox error carrying the backend's lifecycle status details."""

    def __init__(
        self,
        message: str,
        *,
        sandbox_id: str | None = None,
        status: str | None = None,
        status_message: str | None = None,
        build_id: str | None = None,
    ) -> None:
        self.sandbox_id = sandbox_id
        self.status = status
        self.status_message = status_message
        self.build_id = build_id
        super().__init__(message)


class SandboxTerminatedError(SandboxStatusError):
    """Raised when a sandbox has terminated."""


class SandboxBuildError(SandboxStatusError):
    """Raised when a custom sandbox image build fails."""


class SandboxCreationError(SandboxStatusError):
    """Raised when sandbox creation or startup fails."""


class ExecError(SandboxError):
    """Raised when command execution fails."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SandboxInfo:
    """Information about a sandbox."""

    id: str
    """Sandbox identifier."""

    status: str
    """Current lifecycle status of the sandbox."""

    created_at: str
    """Timestamp when the sandbox was created."""

    name: str | None = None
    """Optional human-readable name for the sandbox."""

    build_id: str | None = None
    """Optional identifier of the image build associated with this sandbox."""

    status_message: str | None = None
    """Backend-provided explanation for the current lifecycle status."""


@dataclass
class ExecResult:
    """Result of a completed command execution."""

    stdout: bytes
    """Raw bytes written to standard output by the command."""

    stderr: bytes
    """Raw bytes written to standard error by the command."""

    exit_code: int | None
    """Exit code of the process, if it exited normally."""

    signal: int | None = None
    """Signal number that terminated the process, if any."""

    error: str | None = None
    """Error string if execution failed."""

    @property
    def stdout_text(self) -> str:
        """:attr:`stdout` decoded as UTF-8 (with ``errors="replace"``)."""
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        """:attr:`stderr` decoded as UTF-8 (with ``errors="replace"``)."""
        return self.stderr.decode("utf-8", errors="replace")


@dataclass
class ExecEvent:
    """A single event from a streaming exec call."""

    session_id: str | None = None
    """V2 session identifier, set when the process session starts."""

    stdout: str | None = None
    """Stdout text chunk, if this event carries stdout output."""

    stderr: str | None = None
    """Stderr text chunk, if this event carries stderr output."""

    exit_code: int | None = None
    """Exit code of the process, set on the process-exited event."""

    exit_signal: int | None = None
    """Signal number that terminated the process, if any."""

    error_code: str | None = None
    """Error code string, set on error events."""

    error_message: str | None = None
    """Error message string, set on error events."""

    @property
    def is_started(self) -> bool:
        return self.session_id is not None

    @property
    def is_output(self) -> bool:
        return self.stdout is not None or self.stderr is not None

    @property
    def is_exited(self) -> bool:
        return self.exit_code is not None or self.exit_signal is not None

    @property
    def is_error(self) -> bool:
        return self.error_code is not None


@dataclass
class PythonSkippedArtifact:
    """An artifact the execution produced but the server did not persist."""

    name: str
    """The artifact's declared name."""

    reason: str
    """Why the artifact was skipped (e.g. over the byte budget)."""

    byte_size: int = 0
    """Declared size of the skipped artifact, when known."""


@dataclass
class PythonExecution:
    """Result of :meth:`Sandbox.execute_python`."""

    execution_id: str
    """Identifier of this execution; artifacts reference it in their origin."""

    stdout: bytes
    """Raw bytes the source wrote to standard output."""

    stderr: bytes
    """Raw bytes the source wrote to standard error."""

    exit_code: int
    """Exit code of the python process."""

    outcome: str
    """``"succeeded"``, ``"failed"``, ``"timed_out"``, or ``"interrupted"``."""

    error_message: str = ""
    """Failure detail when the execution failed outside the process's own exit."""

    artifacts: list[Artifact] = field(default_factory=list)
    """Artifacts persisted from this execution (captured charts, saved files)."""

    skipped_artifacts: list[PythonSkippedArtifact] = field(default_factory=list)
    """Artifacts the execution produced that were not persisted, with reasons."""

    @property
    def stdout_text(self) -> str:
        """:attr:`stdout` decoded as UTF-8 (with ``errors="replace"``)."""
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        """:attr:`stderr` decoded as UTF-8 (with ``errors="replace"``)."""
        return self.stderr.decode("utf-8", errors="replace")

    @property
    def succeeded(self) -> bool:
        return self.outcome == "succeeded"

    @property
    def charts(self) -> list[Artifact]:
        """The chart artifacts captured from the execution's namespace."""
        return [a for a in self.artifacts if a.kind == "chart"]


_PYTHON_OUTCOME_NAMES: dict[int, str] = {
    runtime_pb.PythonExecutionOutcome.SUCCEEDED: "succeeded",
    runtime_pb.PythonExecutionOutcome.FAILED: "failed",
    runtime_pb.PythonExecutionOutcome.TIMED_OUT: "timed_out",
    runtime_pb.PythonExecutionOutcome.INTERRUPTED: "interrupted",
}


@dataclass
class ExecProcess:
    """Handle to a running exec stream, allowing stdin writes and signal sends."""

    _request_queue: queue.Queue = field(repr=False)
    _response_iterator: Iterator[process_pb.OpenSessionResponse] = field(repr=False)
    _sandbox_id: str = field(repr=False)
    _client: SandboxClient = field(repr=False)
    _stdin_closed: bool = field(default=False, repr=False)
    _last_seq: int = field(default=0, repr=False)

    session_id: str | None = None
    """V2 session identifier once the process session starts."""

    def write_stdin(self, data: str | bytes) -> None:
        """Send data to the process's stdin.

        Parameters
        ----------
        data
            Bytes or string to write to stdin. Strings are UTF-8 encoded.
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._request_queue.put(
            process_pb.OpenSessionRequest(
                command=Oneof("stdin_data", process_pb.StdinData(data=data))
            )
        )

    def close_stdin(self) -> None:
        """Signal EOF on stdin."""
        if self._stdin_closed:
            return
        self._stdin_closed = True
        self._request_queue.put(
            process_pb.OpenSessionRequest(
                command=Oneof("stdin_eof", process_pb.StdinEof())
            )
        )

    def send_signal(self, sig: int = signal.SIGTERM) -> None:
        """Send a signal to the process.

        Parameters
        ----------
        sig
            Signal number to send. Defaults to ``SIGTERM``.
        """
        self._request_queue.put(
            process_pb.OpenSessionRequest(
                command=Oneof("signal", process_pb.SessionSignal(signal=sig))
            )
        )

    def output(self) -> Iterator[ExecEvent]:
        """Iterate over output events from the process.

        Yields
        ------
        ExecEvent
            Events describing process start, stdout/stderr chunks,
            process exit, or errors.
        """
        for response in self._response_iterator:
            if response.seq:
                self._last_seq = max(self._last_seq, response.seq)
            event = _response_to_event(response)
            if event.session_id is not None:
                self.session_id = event.session_id
            yield event
            if event.is_exited or event.is_error:
                return

    def wait(self, *, auto_reattach: bool = True) -> ExecResult:
        """Consume all output and return the final result.

        If the response stream is interrupted after the session starts,
        reconnect and replay buffered events by default. Set
        ``auto_reattach=False`` to handle transport disconnects yourself.

        Returns
        -------
        ExecResult
            Aggregated stdout, stderr, exit code, and signal information.
        """
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        exit_code: int | None = None
        exit_signal: int | None = None
        error: str | None = None
        completed = False
        transient_codes = {
            Code.CANCELED,
            Code.UNKNOWN,
            Code.DEADLINE_EXCEEDED,
            Code.RESOURCE_EXHAUSTED,
            Code.ABORTED,
            Code.INTERNAL,
            Code.UNAVAILABLE,
            Code.DATA_LOSS,
        }

        while not completed:
            try:
                for response in self._response_iterator:
                    if response.seq and response.seq <= self._last_seq:
                        continue
                    if response.seq:
                        self._last_seq = response.seq

                    match response.event:
                        case Oneof(field="session_attached", value=attached):
                            self.session_id = attached.session_id
                        case Oneof(field="output_data", value=od):
                            if od.stream == process_pb.OutputData.Stream.STDOUT:
                                stdout_parts.append(od.data)
                            elif od.stream == process_pb.OutputData.Stream.STDERR:
                                stderr_parts.append(od.data)
                        case Oneof(field="process_exited", value=pe):
                            if pe.has_field("exit_code"):
                                exit_code = pe.exit_code
                            if pe.has_field("signal"):
                                exit_signal = pe.signal
                            completed = True
                        case Oneof(field="process_timed_out", value=timed_out):
                            if timed_out.has_field("exit_code"):
                                exit_code = timed_out.exit_code
                            if timed_out.has_field("signal"):
                                exit_signal = timed_out.signal
                            error = "timed_out: Process timed out"
                            completed = True
                        case Oneof(field="process_failed", value=failed):
                            error = f"process_failed: {failed.message}"
                            completed = True
                        case Oneof(field="error", value=event):
                            error = f"{event.code}: {event.message}"
                            completed = True
                    if completed:
                        break
            except ConnectRpcError as exc:
                if (
                    not auto_reattach
                    or self.session_id is None
                    or exc.code not in transient_codes
                ):
                    raise

            if completed:
                self._request_queue.put(None)
                break
            if not auto_reattach or self.session_id is None:
                raise SandboxError("Exec stream ended before the process completed")

            time.sleep(1)
            self._request_queue.put(None)
            request_queue: queue.Queue[process_pb.OpenSessionRequest | None] = (
                queue.Queue()
            )
            request_queue.put(
                process_pb.OpenSessionRequest(
                    command=Oneof(
                        "attach_session",
                        process_pb.AttachSession(
                            sandbox_id=self._sandbox_id,
                            session_id=self.session_id,
                            after_seq=self._last_seq,
                        ),
                    )
                )
            )
            if self._stdin_closed:
                request_queue.put(
                    process_pb.OpenSessionRequest(
                        command=Oneof("stdin_eof", process_pb.StdinEof())
                    )
                )

            def request_iterator() -> Iterator[process_pb.OpenSessionRequest]:
                while True:
                    request = request_queue.get()
                    if request is None:
                        return
                    yield request

            self._request_queue = request_queue
            self._response_iterator = self._client._process_stub.open_session(
                request_iterator(), headers=self._client._headers
            )

        return ExecResult(
            stdout=b"".join(stdout_parts),
            stderr=b"".join(stderr_parts),
            exit_code=exit_code,
            signal=exit_signal,
            error=error,
        )


# ---------------------------------------------------------------------------
# Sandbox handle
# ---------------------------------------------------------------------------


class Sandbox:
    """A managed sandbox backed by the Chalk ``SandboxService``.

    Create sandboxes with :meth:`SandboxClient.create`. Use :meth:`exec` to run
    commands and :meth:`terminate` to shut the sandbox down when you're done.

    Examples
    --------
    >>> from chalkcompute import Image, SandboxClient
    >>> with SandboxClient.from_env() as client:
    ...     sandbox = client.create(
    ...         Image.debian_slim().pip_install(["numpy"]),
    ...     )
    ...     result = sandbox.exec(
    ...         "python",
    ...         "-c",
    ...         "import numpy; print(numpy.__version__)",
    ...     )
    ...     sandbox.terminate()
    >>> result.stdout_text.strip()
    '2.5.1'
    """

    __chalk_type_id__ = "chalkcompute.Sandbox"

    __chalk_arrow_type__ = arrow_pb.ArrowType(
        arrow_type_enum=Oneof(
            "struct",
            arrow_pb.Struct(
                sub_field_types=[
                    arrow_pb.Field(
                        name="sandbox_id",
                        arrow_type=arrow_pb.ArrowType(
                            arrow_type_enum=Oneof("large_utf8", arrow_pb.EmptyMessage())
                        ),
                        nullable=False,
                    )
                ]
            ),
        )
    )

    @staticmethod
    def __chalk_pyarrow_type__() -> Any:
        import pyarrow as pa

        return pa.struct([pa.field("sandbox_id", pa.large_utf8(), nullable=False)])

    def __chalk_serialize__(self) -> Any:
        """Transport the existing sandbox ID, never its credentials or spec."""
        import pyarrow as pa

        return pa.scalar(
            {"sandbox_id": self._require_started()},
            type=self.__chalk_pyarrow_type__(),
        )

    @classmethod
    def __chalk_deserialize__(cls, value: Any) -> Sandbox:
        """Restore a handle without I/O; operations use the receiver's identity."""
        if not value.is_valid or not value.type.equals(cls.__chalk_pyarrow_type__()):
            raise ValueError("Invalid Sandbox Arrow scalar")
        sandbox_id = value["sandbox_id"].as_py()
        if not isinstance(sandbox_id, str) or not sandbox_id.strip():
            raise ValueError("Invalid Sandbox ID")
        return cls(sandbox_id=sandbox_id)

    def __init__(
        self,
        *,
        image: str | Image | None = None,
        name: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        gpu: str | None = None,
        env: dict[str, str] | None = None,
        secrets: list[Secret] | None = None,
        volumes: _VolumeList | None = None,
        port: int | None = None,
        entrypoint: list[str] | None = None,
        lifetime: str | None = None,
        security_policy: ContainerSecurityPolicy | None = None,
        network_policy: NetworkPolicy | None = None,
        compute_class: ComputeClass | None = None,
        tags: dict[str, str] | None = None,
        restart_policy: RestartPolicy | None = None,
        chalk_identity: bool = False,
        managed_ssh: Mapping[str, SSHDest] | None = None,
        chalk_client: Any | None = None,
        sandbox_client: SandboxClient | None = None,
        client: Any | None = None,
        sandbox_id: str | None = None,
    ) -> None:
        """Configure a sandbox to be created by :meth:`run`.

        Parameters
        ----------
        image
            Container image to use, either a string reference such as
            ``"ubuntu:latest"`` or a declarative :class:`Image`.
        name
            Optional DNS-safe sandbox name.
        cpu
            Optional CPU limit, for example ``"2"`` or ``"500m"``.
        memory
            Optional memory limit, for example ``"4Gi"`` or ``"512Mi"``.
        gpu
            Optional GPU request, for example ``"nvidia-l4"``.
        env
            Optional environment variables to set in the sandbox.
        secrets
            Optional :class:`~chalkcompute.Secret` references to inject.
        volumes
            Optional ``Volume`` handles, ``VolumeMount`` specifications, or
            ``(name_or_volume, mount_path)`` pairs to mount into the sandbox.
            Volume handles preserve their selected ref or pinned version.
            Each volume must already exist; :meth:`run` fails if one is missing.
        port
            Optional published port.
        entrypoint
            Optional command to run as the sandbox container's main process.
        lifetime
            Optional max lifetime as a protobuf duration string
            (for example ``"30s"`` or ``"300s"``).
        security_policy
            Optional :class:`ContainerSecurityPolicy`.
        network_policy
            Optional network egress policy. Reuses the same
            :class:`NetworkPolicy` type accepted by :class:`Container`.
        compute_class
            Optional :class:`ComputeClass` (``K8S`` or ``HOST``).
        tags
            Optional labels attached to the sandbox spec.
        restart_policy
            Optional restart behavior. Use :attr:`RestartPolicy.NEVER` or
            :attr:`RestartPolicy.ALWAYS`.
        chalk_identity
            Whether to give the sandbox a platform-managed Chalk identity.
            The identity is exposed inside the sandbox through the standard
            ``CHALK_WEB_IDENTITY_TOKEN_FILE`` environment variable.
        managed_ssh
            Optional SSH aliases mapped to :class:`SSHDest` instances. Managed
            SSH sandboxes run on host compute.

        sandbox_id
            Existing sandbox ID. With no client, authentication is resolved lazily
            from the receiver's environment on the first operation. This form
            never creates a sandbox and cannot be mixed with image configuration.

        Notes
        -----
        Calling :meth:`run` without a client creates one from the environment.
        Prefer creating a :class:`SandboxClient` explicitly and calling
        :meth:`SandboxClient.create` instead of constructing a ``Sandbox``
        directly.

        Examples
        --------
        >>> with SandboxClient.from_env() as client:
        ...     sandbox = client.create("ubuntu:latest")
        """
        self._client: Any | None = None
        self._id: str | None = None
        self._info: SandboxInfo | None = None
        self._terminated = False

        self._image: str | Image | None = None
        self._name: str | None = None
        self._cpu: str | None = None
        self._memory: str | None = None
        self._gpu: str | None = None
        self._env: dict[str, str] = {}
        self._secrets: list[Secret] = []
        self._resolved_secrets: list[Secret] = []
        # Existence is checked at run() time, not here.
        self._referenced_volumes: list[VolumeMount] = []
        # Populated at run() time: referenced volumes plus any freshly
        # uploaded volumes.
        self._volumes: list[VolumeMount] = []
        self._volume_client: VolumeClient | None = None
        self._port: int | None = None
        self._entrypoint: list[str] | None = None
        self._lifetime: str | None = None
        self._security_policy: ContainerSecurityPolicy | None = None
        self._network_policy: NetworkPolicy | None = None
        self._compute_class: ComputeClass | None = None
        self._tags: dict[str, str] = {}
        self._restart_policy: RestartPolicy | None = None
        self._chalk_identity = False
        self._managed_ssh: dict[str, SSHDest] = {}
        self._chalk_client: Any | None = None
        self._built_image_uri: str | None = None

        if client is not None or sandbox_id is not None:
            if (
                image is not None
                or name is not None
                or cpu is not None
                or memory is not None
                or gpu is not None
                or env is not None
                or secrets is not None
                or volumes is not None
                or port is not None
                or entrypoint is not None
                or lifetime is not None
                or security_policy is not None
                or network_policy is not None
                or compute_class is not None
                or tags is not None
                or restart_policy is not None
                or chalk_identity
                or managed_ssh is not None
                or chalk_client is not None
                or sandbox_client is not None
            ):
                raise TypeError(
                    "Sandbox(client=..., sandbox_id=...) cannot be combined with "
                    "Sandbox(image=...) configuration arguments"
                )
            if not isinstance(sandbox_id, str) or not sandbox_id.strip():
                raise ValueError("Sandbox requires a nonempty sandbox_id")
            self._client = client
            self._id = sandbox_id
            return

        if image is None:
            raise TypeError("Sandbox() missing required keyword-only argument: 'image'")
        ContainerSpec.validate_fields(
            image=image,
            name=name,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            env=env,
            entrypoint=entrypoint,
            secrets=secrets,
            port=port,
            lifetime=lifetime,
            security_policy=security_policy,
            network_policy=network_policy,
            compute_class=compute_class,
            tags=tags,
            restart_policy=restart_policy,
            chalk_identity=chalk_identity,
            managed_ssh=managed_ssh,
        )

        self._client = sandbox_client
        self._image = image
        self._name = name
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        self._env = dict(env) if env else {}
        self._secrets = list(secrets or [])
        self._referenced_volumes = normalize_volume_refs(volumes)
        self._port = port
        self._entrypoint = list(entrypoint) if entrypoint is not None else None
        self._lifetime = lifetime
        self._security_policy = security_policy
        self._network_policy = network_policy
        self._compute_class = compute_class
        self._tags = dict(tags or {})
        self._restart_policy = restart_policy
        self._chalk_identity = chalk_identity
        self._managed_ssh = dict(managed_ssh or {})
        self._chalk_client = chalk_client

    @classmethod
    def from_id(cls, sandbox_id: str, *, chalk_client: Any | None = None) -> Sandbox:
        """Attach to an existing sandbox by ID."""
        client = SandboxClient.from_env(chalk_client=chalk_client)
        sandbox = client.get(sandbox_id)
        sandbox.refresh()
        return sandbox

    @classmethod
    def from_name(cls, name: str, *, chalk_client: Any | None = None) -> Sandbox:
        """Attach to an existing sandbox by name."""
        client = SandboxClient.from_env(chalk_client=chalk_client)
        matches = [info for info in client.list() if info.name == name]
        if not matches:
            raise SandboxNotFoundError(f"Sandbox {name!r} was not found")
        sandbox = client.get(matches[0].id)
        sandbox._info = matches[0]
        sandbox._name = name
        return sandbox

    @classmethod
    def list_all(cls, *, chalk_client: Any | None = None) -> list[SandboxInfo]:
        """List all sandboxes in the current environment."""
        client = SandboxClient.from_env(chalk_client=chalk_client)
        try:
            return client.list()
        finally:
            client.close()

    @property
    def id(self) -> str | None:
        """Opaque sandbox identifier assigned by the service.

        ``None`` until :meth:`run` has created the sandbox.
        """
        return self._id

    @property
    def info(self) -> SandboxInfo | None:
        """The most recently fetched :class:`SandboxInfo` snapshot.

        ``None`` until the sandbox has been started or attached to.
        """
        if self._info is None and self._id is not None:
            self._info = self._fetch_info()
        return self._info

    @property
    def image_uri(self) -> str | None:
        """The fully qualified registry URI of the image the sandbox uses.

        Set after :meth:`run` finishes the image build (or, if the sandbox
        was constructed with a string image reference, equal to that
        reference). ``None`` before the sandbox has been started.
        """
        return self._built_image_uri

    def refresh(self) -> SandboxInfo:
        """Force-refresh sandbox info from the server."""
        self._info = self._fetch_info()
        return self._info

    def _fetch_info(self) -> SandboxInfo:
        client = self._require_client()
        sandbox_id = self._require_started()
        try:
            resp = client._stub.get_sandbox(
                pb.GetSandboxRequest(id=sandbox_id),
                headers=client._headers,
            )
        except ConnectRpcError as e:
            _handle_rpc_error(e, sandbox_id)
            raise
        return _pb_to_sandbox_info(resp.sandbox)

    def mount_volume(self, name: str | Volume, mount_path: str) -> Sandbox:
        """Mount a named volume or volume handle into the sandbox.

        Must be called before :meth:`run`. Returns ``self`` for chaining.

        Examples
        --------
        >>> from chalkcompute import Sandbox
        >>> sandbox = (
        ...     Sandbox(image="ubuntu:latest")
        ...     .mount_volume("my-data", "/data")
        ...     .run()
        ... )
        """
        self._referenced_volumes.extend(normalize_volume_refs([(name, mount_path)]))
        return self

    @_instrumented("chalkcompute.sandbox.run")
    def run(
        self,
        *,
        wait: bool = True,
        poll_interval: float = 0.5,
        timeout: float = 600.0,
    ) -> Sandbox:
        """Create the sandbox and optionally wait until it is ready.

        Parameters
        ----------
        wait
            If ``True``, poll until the sandbox is ready (default ``True``).
        poll_interval
            Seconds between readiness polls.
        timeout
            Maximum seconds to wait for the sandbox to become ready.

        Returns
        -------
        Sandbox
            ``self``, to allow chaining.
        """
        if self._id is not None:
            raise SandboxError(
                "Sandbox is already running. Call .terminate() before calling .run() again."
            )
        if self._image is None:
            raise SandboxError("Sandbox has no image configured.")

        client = self._client
        if client is None:
            client = SandboxClient.from_env(chalk_client=self._chalk_client)

        image = self._image
        if isinstance(image, Image):
            image = self._build_image(
                client,
                poll_interval=poll_interval,
                timeout=timeout,
            )
        self._built_image_uri = image

        # A missing user-referenced volume fails run().
        connect = self._connect_client(client)
        self._volumes = [
            referenced_volume_mount(connect, mount)
            for mount in self._referenced_volumes
        ]
        # Upload the image's lazy local files (add_local_file / add_local_dir
        # with the default strategy="volume") and mount them alongside.
        lazy_files = (
            self._image.lazy_local_files if isinstance(self._image, Image) else set()
        )
        if lazy_files:
            assert isinstance(self._image, Image)
            self._volume_client = VolumeClient.from_connect(connect)
            self._volumes += self._volume_client.upload_lazy_files(
                self._image,
                volume_prefix="sandbox-files",
            )

        resolved_secrets = self._secrets
        if any(s.is_lazy for s in self._secrets):
            scope_name = self._name or f"sandbox-{uuid.uuid4().hex[:8]}"
            resolved_secrets, _upserted = resolve_lazy_secrets(
                self._secrets, connect, scope=SecretScope.sandbox(scope_name)
            )
        self._resolved_secrets = resolved_secrets

        client._instantiate(
            self,
            image,
            wait=wait,
            poll_interval=poll_interval,
            timeout=timeout,
        )
        self._client = client
        self._terminated = False

        if lazy_files:
            self._wait_for_volume_files(timeout=120.0, poll_interval=5.0)
        return self

    def _require_client(self) -> Any:
        if self._client is None:
            self._require_started()
            # Deserialized handles bind to the receiver, never the creator's token.
            self._client = SandboxClient.from_env()
        return self._client

    def _require_started(self) -> str:
        if self._id is None:
            raise SandboxError("Sandbox is not running. Call .run() first.")
        return self._id

    @_instrumented("chalkcompute.sandbox.exec")
    def exec(
        self,
        command: str,
        *args: str,
        timeout_secs: int | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        auto_reattach: bool = True,
    ) -> ExecResult:
        """Execute a command and wait for it to complete.

        Parameters
        ----------
        command
            Command binary or script to execute.
        *args
            Arguments to pass to the command.
        timeout_secs
            Optional timeout in seconds.
        workdir
            Optional working directory inside the sandbox.
        env
            Optional environment variables to set for the command.
        auto_reattach
            Reattach and replay buffered output after transient transport
            disconnects. Disable to handle disconnects yourself.

        Returns
        -------
        ExecResult
            Result with stdout/stderr and exit code.
        """
        self._require_started()
        process = self.exec_start(
            command,
            *args,
            timeout_secs=timeout_secs,
            workdir=workdir,
            env=env,
        )
        process.close_stdin()
        return process.wait(auto_reattach=auto_reattach)

    def exec_stream(
        self,
        command: str,
        *args: str,
        timeout_secs: int | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Iterator[ExecEvent]:
        """Execute a command and stream events as they arrive.

        Parameters
        ----------
        command
            Command binary or script to execute.
        *args
            Arguments to pass to the command.
        timeout_secs
            Optional timeout in seconds.
        workdir
            Optional working directory inside the sandbox.
        env
            Optional environment variables to set for the command.

        Yields
        ------
        ExecEvent
            Events for process start, stdout/stderr output, process exit,
            and errors.
        """
        self._require_started()
        process = self.exec_start(
            command,
            *args,
            timeout_secs=timeout_secs,
            workdir=workdir,
            env=env,
        )
        process.close_stdin()
        yield from process.output()

    def exec_start(
        self,
        command: str,
        *args: str,
        timeout_secs: int | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecProcess:
        """Start a command and return an ``ExecProcess`` handle for interactive use.

        The caller can write to stdin, send signals, and iterate output.

        Parameters
        ----------
        command
            Command binary or script to execute.
        *args
            Arguments to pass to the command.
        timeout_secs
            Optional timeout in seconds.
        workdir
            Optional working directory inside the sandbox.
        env
            Optional environment variables to set for the command.

        Returns
        -------
        ExecProcess
            Handle for interacting with the running process.
        """
        client = self._require_client()
        sandbox_id = self._require_started()
        init = process_pb.NewProcess(
            sandbox_id=sandbox_id,
            command=command,
            args=list(args),
        )
        if workdir is not None:
            init.workdir = workdir
        validate_env(env)
        if env is not None:
            for k, v in env.items():
                init.env_vars[k] = v
        if timeout_secs is not None:
            init.timeout = Duration(seconds=timeout_secs)

        request_queue: queue.Queue[process_pb.OpenSessionRequest | None] = queue.Queue()
        # Send the init message first
        request_queue.put(
            process_pb.OpenSessionRequest(command=Oneof("new_process", init))
        )

        def request_iterator() -> Iterator[process_pb.OpenSessionRequest]:
            while True:
                req = request_queue.get()
                if req is None:
                    return
                yield req

        try:
            response_iterator = client._process_stub.open_session(
                request_iterator(), headers=client._headers
            )
        except ConnectRpcError as e:
            _handle_rpc_error(e, sandbox_id)
            raise

        return ExecProcess(
            _request_queue=request_queue,
            _response_iterator=response_iterator,
            _sandbox_id=sandbox_id,
            _client=client,
        )

    @_instrumented("chalkcompute.sandbox.execute_python")
    def execute_python(
        self,
        source: str,
        *,
        timeout_secs: int | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        capture_charts: bool = True,
        max_artifact_bytes: int | None = None,
    ) -> PythonExecution:
        """Run Python source in the sandbox with structured output capture.

        The server wraps ``source`` in a capture harness that injects a
        ``save_artifact(obj, *, name=None, kind=None, content_type=None)``
        helper into the namespace and, after the source finishes, inspects the
        namespace for plotly/altair figures bound to variables. Everything
        saved or captured is persisted to the Chalk artifact store
        server-side — the sandbox itself never receives credentials — and
        returned as :class:`Artifact` handles.

        Parameters
        ----------
        source
            Python source to execute. Bare final expressions do not print;
            charts are captured regardless of printing.
        timeout_secs
            Optional timeout in seconds for the python process.
        workdir
            Optional working directory inside the sandbox.
        env
            Optional environment variables for the process.
        capture_charts
            Inspect the namespace for plotly/altair figures after execution
            (default ``True``).
        max_artifact_bytes
            Byte budget across all artifacts from this execution; artifacts
            past it are reported in ``skipped_artifacts``.

        Returns
        -------
        PythonExecution
            Buffered stdout/stderr, exit status, and persisted artifacts.
        """
        client = self._require_client()
        sandbox_id = self._require_started()
        spec = runtime_pb.ExecutePythonSpec(
            sandbox_id=sandbox_id,
            source=source,
            capture_charts=capture_charts,
        )
        if timeout_secs is not None:
            spec.timeout_secs = timeout_secs
        if workdir is not None:
            spec.workdir = workdir
        validate_env(env)
        if env is not None:
            for k, v in env.items():
                spec.env[k] = v
        if max_artifact_bytes is not None:
            spec.max_artifact_bytes = max_artifact_bytes

        def request_iterator() -> Iterator[runtime_pb.ExecutePythonRequest]:
            yield runtime_pb.ExecutePythonRequest(message=Oneof("spec", spec))

        artifact_client = ArtifactClient(
            client._address,
            use_tls=client._use_tls,
            metadata=client._metadata,
        )

        execution_id = ""
        stdout: list[bytes] = []
        stderr: list[bytes] = []
        artifacts: list[Artifact] = []
        exit_code = -1
        outcome = "failed"
        error_message = ""
        skipped: list[PythonSkippedArtifact] = []
        try:
            for resp in client._runtime_stub.execute_python(
                request_iterator(), headers=client._headers
            ):
                if resp.message is None:
                    continue
                which = resp.message.field
                message = resp.message.value
                if which == "started":
                    execution_id = message.execution_id
                elif which == "output":
                    if message.stream == runtime_pb.PythonOutput.Stream.STDERR:
                        stderr.append(message.data)
                    else:
                        stdout.append(message.data)
                elif which == "artifact":
                    artifacts.append(_artifact_from_proto(message, artifact_client))
                elif which == "completed":
                    completed = message
                    exit_code = completed.exit_code
                    outcome = _PYTHON_OUTCOME_NAMES.get(completed.outcome, "failed")
                    error_message = completed.error_message
                    skipped = [
                        PythonSkippedArtifact(
                            name=s.name, reason=s.reason, byte_size=s.byte_size
                        )
                        for s in completed.skipped_artifacts
                    ]
        except ConnectRpcError as e:
            _handle_rpc_error(e, sandbox_id)
            raise

        return PythonExecution(
            execution_id=execution_id,
            stdout=b"".join(stdout),
            stderr=b"".join(stderr),
            exit_code=exit_code,
            outcome=outcome,
            error_message=error_message,
            artifacts=artifacts,
            skipped_artifacts=skipped,
        )

    @_instrumented("chalkcompute.sandbox.terminate")
    def terminate(self, grace_period_seconds: int | None = None) -> None:
        """Terminate this sandbox.

        Parameters
        ----------
        grace_period_seconds
            Optional grace period to allow running processes to shut down.
        """
        if self._terminated:
            return
        client = self._require_client()
        sandbox_id = self._require_started()
        req = pb.TerminateSandboxRequest(
            id=sandbox_id,
            grace_period=Duration(seconds=grace_period_seconds)
            if grace_period_seconds is not None
            else None,
        )
        try:
            client._stub.terminate_sandbox(req, headers=client._headers)
        except ConnectRpcError as e:
            _handle_rpc_error(e, sandbox_id)
            raise
        self._terminated = True
        if self._volumes:
            volume_client = self._volume_client or VolumeClient.from_connect(
                self._connect_client(client)
            )
            cleanup_owned_volumes(
                volume_client, self._volumes, self._referenced_volumes
            )
        self._volumes = []
        self._volume_client = None
        if self._image is not None:
            self._id = None
            self._info = None

    def _connect_client(self, client: SandboxClient) -> ConnectClient:
        """Return the authenticated Connect client backing ``client``."""
        return getattr(client, "_connect", None) or ConnectClient(
            chalk_client=self._chalk_client
        )

    def _build_image(
        self,
        client: SandboxClient,
        *,
        poll_interval: float,
        timeout: float,
    ) -> str:
        """Build the configured image and return the resulting image URI."""
        assert isinstance(self._image, Image)
        connect = self._connect_client(client)
        try:
            return build_image(
                connect,
                self._image,
                poll_interval=poll_interval,
                timeout=timeout,
            )
        except ConnectError as e:
            raise SandboxBuildError(str(e)) from e

    def _wait_for_volume_files(
        self,
        *,
        timeout: float = 120.0,
        poll_interval: float = 0.5,
    ) -> None:
        """Poll until at least one uploaded file is visible in the ChalkFS mount."""
        assert isinstance(self._image, Image)
        sample = next(iter(self._image.lazy_local_files))
        probe_path = sample.dest
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.exec("test", "-f", probe_path)
            if result.exit_code == 0:
                return
            time.sleep(poll_interval)
        raise SandboxError(
            f"ChalkFS files not visible after {timeout}s (probed {probe_path})"
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


_SandboxInfoList = list[SandboxInfo]
# Defined before SandboxClient, which has a ``.list()`` method that would
# otherwise shadow the builtin ``list`` in class-scope name resolution.
_StrList = list[str]


class SandboxClient:
    """Client for the Sandbox service."""

    def __init__(
        self,
        target: str = "localhost:50051",
        *,
        credentials: object | None = None,
        use_tls: bool | None = None,
        metadata: _GrpcMetadata | None = None,
        _connect: ConnectClient | None = None,
    ) -> None:
        """Initialize the RPC client.

        Parameters
        ----------
        target
            RPC server address (e.g. ``"localhost:50051"`` or
            ``"https://sandbox.example.com"``).
        credentials
            Backward-compatible TLS signal. The object is not inspected; prefer
            ``use_tls`` for new code.
        use_tls
            Whether to use HTTPS for the ConnectRPC transport. When omitted,
            this remains compatible with the old behavior: passing any
            ``credentials`` object enables TLS.
        metadata
            Optional default metadata to send with every RPC call.
        """
        self._target = target
        self._static_metadata = metadata
        self._connect = _connect
        self._use_tls = credentials is not None if use_tls is None else use_tls
        self._address = rpc_address_from_target(target, use_tls=self._use_tls)
        interceptors = rpc_interceptors()
        self._stub = pb_connect.SandboxServiceClientSync(
            self._address,
            protocol=ProtocolType.GRPC,
            interceptors=interceptors,
            http_client=SYNC_HTTP_CLIENT,
            read_max_bytes=RPC_READ_MAX_BYTES,
        )
        self._process_stub = process_connect.SandboxProcessServiceClientSync(
            self._address,
            protocol=ProtocolType.GRPC,
            interceptors=interceptors,
            http_client=SYNC_HTTP_CLIENT,
            read_max_bytes=RPC_READ_MAX_BYTES,
        )
        self._runtime_stub = runtime_connect.SandboxRuntimeServiceClientSync(
            self._address,
            protocol=ProtocolType.GRPC,
            interceptors=interceptors,
            http_client=SYNC_HTTP_CLIENT,
            read_max_bytes=RPC_READ_MAX_BYTES,
        )

    @classmethod
    def from_env(
        cls,
        *,
        chalk_client: Any | None = None,
    ) -> SandboxClient:
        """Create a ``SandboxClient`` from ambient Chalk credentials."""

        connect = ConnectClient(chalk_client=chalk_client)
        connect._ensure_auth()

        host, use_tls = grpc_target_from_url(connect._api_server or "")

        return cls(
            host,
            use_tls=use_tls,
            metadata=connect._rpc_metadata(),
            _connect=(
                connect
                if getattr(connect, "_uses_web_identity_token", lambda: False)() is True
                else None
            ),
        )

    @property
    def _metadata(self) -> _GrpcMetadata | None:
        if self._connect is not None:
            return self._connect._rpc_metadata()
        return self._static_metadata

    @_metadata.setter
    def _metadata(self, value: _GrpcMetadata | None) -> None:
        self._static_metadata = value

    @property
    def _headers(self) -> dict[str, str]:
        return dict(self._metadata or [])

    def __enter__(self) -> SandboxClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP clients."""
        for stub in (self._stub, self._process_stub, self._runtime_stub):
            close = getattr(stub, "close", None)
            if close is not None:
                close()

    def create(
        self,
        image: str | Image,
        *,
        name: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        gpu: str | None = None,
        env: dict[str, str] | None = None,
        secrets: _SecretList | None = None,
        volumes: _VolumeList | None = None,
        port: int | None = None,
        entrypoint: _StrList | None = None,
        lifetime: str | None = None,
        security_policy: ContainerSecurityPolicy | None = None,
        network_policy: NetworkPolicy | None = None,
        compute_class: ComputeClass | None = None,
        tags: dict[str, str] | None = None,
        restart_policy: RestartPolicy | None = None,
        chalk_identity: bool = False,
        managed_ssh: Mapping[str, SSHDest] | None = None,
        wait: bool = True,
        poll_interval: float = 0.5,
        timeout: float = 600.0,
    ) -> Sandbox:
        """Create a new sandbox.

        If ``image`` is an ``Image`` spec, this method builds it first and sends
        the resulting image URI to the sandbox service. If ``wait`` is ``True``
        (the default), this method polls ``GetSandbox`` until the sandbox
        reaches "ready" (or "error").

        Parameters
        ----------
        image
            Container image to use — either a string reference
            (e.g. ``"ubuntu:latest"``) or a declarative ``Image`` builder.
        name
            Optional name for the sandbox.
        cpu
            Optional CPU limit (e.g. ``"1"``, ``"500m"``).
        memory
            Optional memory limit (e.g. ``"512Mi"``, ``"1Gi"``).
        env
            Optional environment variables.
        volumes
            Optional ``Volume`` handles, ``VolumeMount`` specifications, or
            ``(name_or_volume, mount_path)`` pairs to mount into the sandbox.
            Volume handles preserve their selected ref or pinned version.
        entrypoint
            Optional command to run as the sandbox container's main process.
        network_policy
            Optional network egress policy. Reuses the same ``NetworkPolicy``
            type accepted by containers.
        restart_policy
            Optional restart behavior. Use ``RestartPolicy.NEVER`` or
            ``RestartPolicy.ALWAYS``.
        chalk_identity
            Whether to give the sandbox a platform-managed Chalk identity.
            The identity is exposed inside the sandbox through the standard
            ``CHALK_WEB_IDENTITY_TOKEN_FILE`` environment variable.
        managed_ssh
            Optional SSH aliases mapped to :class:`SSHDest` instances.
        wait
            If ``True``, poll until the sandbox is ready (default ``True``).
        poll_interval
            Seconds between ``GetSandbox`` polls (default 0.5).
        timeout
            Maximum seconds to wait for the sandbox to become ready
            (default 600).

        Returns
        -------
        Sandbox
            A sandbox handle.
        """
        return Sandbox(
            image=image,
            name=name,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            env=env,
            secrets=secrets,
            volumes=volumes,
            port=port,
            entrypoint=entrypoint,
            lifetime=lifetime,
            security_policy=security_policy,
            network_policy=network_policy,
            compute_class=compute_class,
            tags=tags,
            restart_policy=restart_policy,
            chalk_identity=chalk_identity,
            managed_ssh=managed_ssh,
            sandbox_client=self,
        ).run(wait=wait, poll_interval=poll_interval, timeout=timeout)

    @_instrumented("chalkcompute.sandbox.create")
    def _instantiate(
        self,
        sandbox: Sandbox,
        image: str,
        *,
        wait: bool,
        poll_interval: float,
        timeout: float,
    ) -> None:
        spec = ContainerSpec(
            image=image,
            name=sandbox._name,
            entrypoint=(
                sandbox._entrypoint
                if sandbox._entrypoint is not None
                else ["sleep", "infinity"]
            ),
            cpu=sandbox._cpu,
            memory=sandbox._memory,
            gpu=sandbox._gpu,
            env=sandbox._env,
            volumes=sandbox._volumes,
            secret_refs=sandbox._resolved_secrets or sandbox._secrets,
            port=sandbox._port,
            lifetime=sandbox._lifetime,
            security_policy=sandbox._security_policy,
            network_policy=sandbox._network_policy,
            compute_class=sandbox._compute_class,
            tags=sandbox._tags,
            restart_policy=sandbox._restart_policy,
            chalk_identity=sandbox._chalk_identity,
            managed_ssh=sandbox._managed_ssh,
        ).to_proto()

        resp = self._stub.create_sandbox(
            pb.CreateSandboxRequest(source=Oneof("spec", spec)),
            headers=self._headers,
        )
        if resp.sandbox is None:
            raise SandboxCreationError("CreateSandbox returned no sandbox")
        info = _pb_to_sandbox_info(resp.sandbox)

        if wait and info.status == "pending":
            info = self._poll_until_ready(
                info.id,
                poll_interval=poll_interval,
                timeout=timeout,
            )
        elif wait and info.status in ("failed", "error", "unknown", "terminated"):
            raise _sandbox_creation_error(info)

        sandbox._id = info.id
        sandbox._info = info

    def _poll_until_ready(
        self,
        sandbox_id: str,
        *,
        poll_interval: float,
        timeout: float,
    ) -> SandboxInfo:
        """Poll GetSandbox until the sandbox reaches a terminal state."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                resp = self._stub.get_sandbox(
                    pb.GetSandboxRequest(id=sandbox_id),
                    headers=self._headers,
                )
            except ConnectRpcError as e:
                _handle_rpc_error(e, sandbox_id)
                raise
            if resp.sandbox is None:
                raise SandboxCreationError("GetSandbox returned no sandbox")
            info = _pb_to_sandbox_info(resp.sandbox)
            if info.status in ("running", "succeeded"):
                return info
            if info.status in ("failed", "error", "unknown", "terminated"):
                raise _sandbox_creation_error(info)
            time.sleep(poll_interval)
        raise SandboxError(
            f"Timed out after {timeout}s waiting for sandbox {sandbox_id} to become ready"
        )

    def get(self, id: str) -> Sandbox:
        """Get a sandbox handle by ID.

        Does not validate the sandbox exists until you call a method on it.

        Parameters
        ----------
        id
            Sandbox identifier.

        Returns
        -------
        Sandbox
            A sandbox handle.
        """
        return Sandbox(client=self, sandbox_id=id)

    def list(self) -> _SandboxInfoList:
        """List all sandboxes.

        Returns
        -------
        list of SandboxInfo
            One entry per sandbox.
        """
        resp = self._stub.list_sandboxes(
            pb.ListSandboxesRequest(), headers=self._headers
        )
        return [_pb_to_sandbox_info(s) for s in resp.sandboxes]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pb_to_sandbox_info(info: pb.SandboxInfo) -> SandboxInfo:
    status = info.status.name.lower()
    return SandboxInfo(
        id=info.id,
        status=status,
        created_at=info.created_at.to_datetime().isoformat()
        if info.created_at is not None
        else "",
        name=info.name or None,
        status_message=(
            info.status_message if info.has_field("status_message") else None
        ),
    )


def _sandbox_creation_error(info: SandboxInfo) -> SandboxStatusError:
    """Build a detailed error from the backend's sandbox status."""
    if info.status == "terminated":
        exc_cls: type[SandboxStatusError] = SandboxTerminatedError
        message = f"Sandbox {info.id} was terminated during creation"
    else:
        exc_cls = SandboxCreationError
        message = f"Sandbox {info.id} entered {info.status} state during creation"
    if info.status_message:
        message = f"{message}: {info.status_message}"
    return exc_cls(
        message,
        sandbox_id=info.id,
        status=info.status,
        status_message=info.status_message,
        build_id=info.build_id,
    )


def _response_to_event(response: process_pb.OpenSessionResponse) -> ExecEvent:
    match response.event:
        case Oneof(field="session_attached", value=attached):
            return ExecEvent(session_id=attached.session_id)
        case Oneof(field="output_data", value=output):
            text = output.data.decode("utf-8", errors="replace")
            if output.stream == process_pb.OutputData.Stream.STDERR:
                return ExecEvent(stderr=text)
            return ExecEvent(stdout=text)
        case Oneof(field="process_exited", value=process_exited):
            return ExecEvent(
                exit_code=(
                    process_exited.exit_code
                    if process_exited.has_field("exit_code")
                    else None
                ),
                exit_signal=(
                    process_exited.signal
                    if process_exited.has_field("signal")
                    else None
                ),
            )
        case Oneof(field="process_timed_out", value=process_timed_out):
            return ExecEvent(
                exit_code=(
                    process_timed_out.exit_code
                    if process_timed_out.has_field("exit_code")
                    else None
                ),
                exit_signal=(
                    process_timed_out.signal
                    if process_timed_out.has_field("signal")
                    else None
                ),
                error_code="timed_out",
                error_message="Process timed out",
            )
        case Oneof(field="process_failed", value=process_failed):
            return ExecEvent(
                error_code="process_failed",
                error_message=process_failed.message,
            )
        case Oneof(field="error", value=error):
            return ExecEvent(
                error_code=error.code,
                error_message=error.message,
            )
        case _:
            return ExecEvent()


def _handle_rpc_error(e: ConnectRpcError, sandbox_id: str) -> None:
    raise_for_rpc_error(
        e,
        {
            Code.NOT_FOUND: (
                SandboxNotFoundError,
                f"Sandbox {sandbox_id} not found",
            ),
            Code.FAILED_PRECONDITION: (
                SandboxTerminatedError,
                f"Sandbox {sandbox_id} has terminated",
            ),
        },
    )


def connect(target: str = "localhost:50051", **kwargs) -> SandboxClient:
    """Create a new ``SandboxClient``.

    Parameters
    ----------
    target
        RPC server address.
    **kwargs
        Additional keyword arguments forwarded to ``SandboxClient``.

    Returns
    -------
    SandboxClient
        A new client instance.
    """
    return SandboxClient(target, **kwargs)


if __name__ == "__main__":
    client = SandboxClient()

    print("Listing sandboxes:")
    for sandbox_info in client.list():
        print(
            f"  - {sandbox_info.id} ({sandbox_info.status}) {sandbox_info.name or ''}"
        )

    sandboxes = client.list()
    if sandboxes:
        sandbox = client.get(id=sandboxes[0].id)
        print(f"\nExecuting 'echo hello' on sandbox {sandbox.id}:")
        result = sandbox.exec("echo", "hello")
        print(f"  stdout: {result.stdout_text.rstrip()}")
        print(f"  exit_code: {result.exit_code}")
