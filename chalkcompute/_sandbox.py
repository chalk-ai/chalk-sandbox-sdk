"""
Python SDK for the Sandbox gRPC Service.

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
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import grpc

from chalkcompute._artifact import Artifact, ArtifactClient, _artifact_from_proto
from chalkcompute._connect import (
    ConnectError,
    ConnectClient,
    build_image,
    grpc_target_from_url,
    raise_for_grpc_error,
)
from chalkcompute._container import NetworkPolicy
from chalkcompute._gen.chalk.container.v1 import service_pb2 as container_pb
from chalkcompute._gen.chalk.sandbox.v1 import runtime_service_pb2 as runtime_pb
from chalkcompute._gen.chalk.sandbox.v1 import (
    runtime_service_pb2_grpc as runtime_pb_grpc,
)
from chalkcompute._gen.chalk.sandbox.v2 import process_service_pb2 as process_pb
from chalkcompute._gen.chalk.sandbox.v2 import (
    process_service_pb2_grpc as process_pb_grpc,
)
from chalkcompute._gen.chalk.sandbox.v2 import service_pb2 as pb
from chalkcompute._gen.chalk.sandbox.v2 import service_pb2_grpc as pb_grpc
from chalkcompute._image import Image
from chalkcompute._naming import validate_dns_label, validate_env
from chalkcompute._tracing import _instrumented
from chalkcompute._volume import (
    MOUNT_TYPE,
    VolumeClient,
    VolumeMount,
    cleanup_owned_volumes,
    normalize_volume_refs,
    referenced_volume_mount,
)

# Aliases defined before SandboxClient, which has a ``.list()`` method that
# would otherwise shadow the builtin ``list`` in class-scope name resolution.
_GrpcMetadata = list[tuple[str, str]]
_VolumeList = list[tuple[str, str]]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SandboxError(Exception):
    """Base exception for sandbox errors."""

    pass


class SandboxNotFoundError(SandboxError):
    """Raised when a sandbox is not found."""

    pass


class SandboxTerminatedError(SandboxError):
    """Raised when a sandbox has terminated."""

    pass


class SandboxBuildError(SandboxError):
    """Raised when a custom sandbox image build fails."""

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


class SandboxCreationError(SandboxError):
    """Raised when sandbox creation or startup fails."""

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


class ExecError(SandboxError):
    """Raised when command execution fails."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class RestartPolicy(Enum):
    """Policy controlling whether a sandbox restarts after exiting."""

    NEVER = "RESTART_POLICY_NEVER"
    ALWAYS = "RESTART_POLICY_ALWAYS"


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
    runtime_pb.PYTHON_EXECUTION_OUTCOME_SUCCEEDED: "succeeded",
    runtime_pb.PYTHON_EXECUTION_OUTCOME_FAILED: "failed",
    runtime_pb.PYTHON_EXECUTION_OUTCOME_TIMED_OUT: "timed_out",
    runtime_pb.PYTHON_EXECUTION_OUTCOME_INTERRUPTED: "interrupted",
}


@dataclass
class ExecProcess:
    """Handle to a running exec stream, allowing stdin writes and signal sends."""

    _request_queue: queue.Queue = field(repr=False)
    _response_iterator: Iterator[process_pb.OpenSessionResponse] = field(repr=False)
    _sandbox_id: str = field(repr=False)

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
            process_pb.OpenSessionRequest(stdin_data=process_pb.StdinData(data=data))
        )

    def close_stdin(self) -> None:
        """Signal EOF on stdin."""
        self._request_queue.put(
            process_pb.OpenSessionRequest(stdin_eof=process_pb.StdinEof())
        )

    def send_signal(self, sig: int = signal.SIGTERM) -> None:
        """Send a signal to the process.

        Parameters
        ----------
        sig
            Signal number to send. Defaults to ``SIGTERM``.
        """
        self._request_queue.put(
            process_pb.OpenSessionRequest(signal=process_pb.SessionSignal(signal=sig))
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
            event = _response_to_event(response)
            if event.session_id is not None:
                self.session_id = event.session_id
            yield event
            if event.is_exited or event.is_error:
                return

    def wait(self) -> ExecResult:
        """Consume all output and return the final result.

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

        for response in self._response_iterator:
            msg_type = response.WhichOneof("event")
            if msg_type == "output_data":
                od = response.output_data
                if od.stream == process_pb.OutputData.STREAM_STDOUT:
                    stdout_parts.append(od.data)
                elif od.stream == process_pb.OutputData.STREAM_STDERR:
                    stderr_parts.append(od.data)
            elif msg_type == "process_exited":
                pe = response.process_exited
                if pe.HasField("exit_code"):
                    exit_code = pe.exit_code
                if pe.HasField("signal"):
                    exit_signal = pe.signal
            elif msg_type == "error":
                error = f"{response.error.code}: {response.error.message}"

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

    def __init__(
        self,
        *,
        image: str | Image | None = None,
        name: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        env: dict[str, str] | None = None,
        volumes: list[tuple[str, str]] | None = None,
        entrypoint: list[str] | None = None,
        network_policy: NetworkPolicy | None = None,
        restart_policy: RestartPolicy | None = None,
        chalk_identity: bool = False,
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
        env
            Optional environment variables to set in the sandbox.
        volumes
            Optional ``(volume_name, mount_path)`` pairs to mount into the
            sandbox. Each volume must already exist; :meth:`run` fails if one
            is missing.
        entrypoint
            Optional command to run as the sandbox container's main process.
        network_policy
            Optional network egress policy. Reuses the same
            :class:`NetworkPolicy` type accepted by :class:`Container`.
        restart_policy
            Optional restart behavior. Use :attr:`RestartPolicy.NEVER` or
            :attr:`RestartPolicy.ALWAYS`.
        chalk_identity
            Whether to give the sandbox a platform-managed Chalk identity.
            The identity is exposed inside the sandbox through the standard
            ``CHALK_WEB_IDENTITY_TOKEN_FILE`` environment variable.

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
        self._env: dict[str, str] = {}
        # Existence is checked at run() time, not here.
        self._referenced_volumes: list[tuple[str, str]] = []
        # Populated at run() time: referenced volumes plus any freshly
        # uploaded volumes.
        self._volumes: list[VolumeMount] = []
        self._volume_client: VolumeClient | None = None
        self._entrypoint: list[str] | None = None
        self._network_policy: NetworkPolicy | None = None
        self._restart_policy: RestartPolicy | None = None
        self._chalk_identity = False
        self._chalk_client: Any | None = None

        if client is not None or sandbox_id is not None:
            if (
                image is not None
                or name is not None
                or cpu is not None
                or memory is not None
                or env is not None
                or volumes is not None
                or entrypoint is not None
                or network_policy is not None
                or restart_policy is not None
                or chalk_identity
                or chalk_client is not None
                or sandbox_client is not None
            ):
                raise TypeError(
                    "Sandbox(client=..., sandbox_id=...) cannot be combined with "
                    "Sandbox(image=...) configuration arguments"
                )
            if client is None or sandbox_id is None:
                raise TypeError(
                    "Sandbox(client=..., sandbox_id=...) requires both arguments"
                )
            self._client = client
            self._id = sandbox_id
            return

        if image is None:
            raise TypeError("Sandbox() missing required keyword-only argument: 'image'")
        if not isinstance(image, (str, Image)):
            raise TypeError(
                f"image must be an Image or image reference string, got {image!r}"
            )
        if name is not None:
            validate_dns_label(name)
        if cpu is not None and not isinstance(cpu, str):
            raise TypeError(f"cpu must be a string such as '2' or '500m', got {cpu!r}")
        if memory is not None and not isinstance(memory, str):
            raise TypeError(
                f"memory must be a string such as '2Gi' or '512Mi', got {memory!r}"
            )
        validate_env(env)
        if network_policy is not None and not isinstance(network_policy, NetworkPolicy):
            raise TypeError("network_policy must be a NetworkPolicy")
        if restart_policy is not None and not isinstance(restart_policy, RestartPolicy):
            raise TypeError("restart_policy must be a RestartPolicy")
        if not isinstance(chalk_identity, bool):
            raise TypeError("chalk_identity must be a bool")

        self._client = sandbox_client
        self._image = image
        self._name = name
        self._cpu = cpu
        self._memory = memory
        self._env = dict(env) if env else {}
        self._referenced_volumes = normalize_volume_refs(volumes)
        self._entrypoint = list(entrypoint) if entrypoint is not None else None
        self._network_policy = network_policy
        self._restart_policy = restart_policy
        self._chalk_identity = chalk_identity
        self._chalk_client = chalk_client

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

    def refresh(self) -> SandboxInfo:
        """Force-refresh sandbox info from the server."""
        self._info = self._fetch_info()
        return self._info

    def _fetch_info(self) -> SandboxInfo:
        client = self._require_client()
        sandbox_id = self._require_started()
        try:
            resp = client._stub.GetSandbox(
                pb.GetSandboxRequest(id=sandbox_id),
                metadata=client._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, sandbox_id)
            raise
        return _pb_to_sandbox_info(resp.sandbox)

    def mount_volume(self, name: str, mount_path: str) -> Sandbox:
        """Mount a named volume into the sandbox.

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
        self._referenced_volumes.append((name, mount_path))
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

        # A missing user-referenced volume fails run().
        connect = self._connect_client(client)
        self._volumes = [
            referenced_volume_mount(connect, name, mount_path)
            for name, mount_path in self._referenced_volumes
        ]
        # Upload the image's lazy local files (add_local_file / add_local_dir
        # with the default strategy="volume") and mount them alongside.
        if isinstance(self._image, Image) and self._image.lazy_local_files:
            self._volume_client = VolumeClient.from_connect(connect)
            self._volumes += self._volume_client.upload_lazy_files(
                self._image,
                volume_prefix="sandbox-files",
            )

        client._instantiate(
            self,
            image,
            wait=wait,
            poll_interval=poll_interval,
            timeout=timeout,
        )
        self._client = client
        self._terminated = False
        return self

    def _require_client(self) -> Any:
        if self._client is None:
            raise SandboxError("Sandbox is not running. Call .run() first.")
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
        return process.wait()

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
            init.timeout.seconds = timeout_secs

        request_queue: queue.Queue[process_pb.OpenSessionRequest | None] = queue.Queue()
        # Send the init message first
        request_queue.put(process_pb.OpenSessionRequest(new_process=init))

        def request_iterator() -> Iterator[process_pb.OpenSessionRequest]:
            while True:
                req = request_queue.get()
                if req is None:
                    return
                yield req

        try:
            response_iterator = client._process_stub.OpenSession(
                request_iterator(), metadata=client._metadata
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, sandbox_id)
            raise

        return ExecProcess(
            _request_queue=request_queue,
            _response_iterator=response_iterator,
            _sandbox_id=sandbox_id,
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
            yield runtime_pb.ExecutePythonRequest(spec=spec)

        stub = runtime_pb_grpc.SandboxRuntimeServiceStub(client._channel)
        artifact_client = ArtifactClient._from_channel(
            client._channel, client._metadata
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
            for resp in stub.ExecutePython(
                request_iterator(), metadata=client._metadata
            ):
                which = resp.WhichOneof("message")
                if which == "started":
                    execution_id = resp.started.execution_id
                elif which == "output":
                    if resp.output.stream == runtime_pb.PythonOutput.STREAM_STDERR:
                        stderr.append(resp.output.data)
                    else:
                        stdout.append(resp.output.data)
                elif which == "artifact":
                    artifacts.append(
                        _artifact_from_proto(resp.artifact, artifact_client)
                    )
                elif which == "completed":
                    completed = resp.completed
                    exit_code = completed.exit_code
                    outcome = _PYTHON_OUTCOME_NAMES.get(completed.outcome, "failed")
                    error_message = completed.error_message
                    skipped = [
                        PythonSkippedArtifact(
                            name=s.name, reason=s.reason, byte_size=s.byte_size
                        )
                        for s in completed.skipped_artifacts
                    ]
        except grpc.RpcError as e:
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
        req = pb.TerminateSandboxRequest(id=sandbox_id)
        if grace_period_seconds is not None:
            req.grace_period.seconds = grace_period_seconds
        try:
            client._stub.TerminateSandbox(req, metadata=client._metadata)
        except grpc.RpcError as e:
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


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


_SandboxInfoList = list[SandboxInfo]
# Defined before SandboxClient, which has a ``.list()`` method that would
# otherwise shadow the builtin ``list`` in class-scope name resolution.
_StrList = list[str]


class SandboxClient:
    """Client for the Sandbox gRPC Service."""

    def __init__(
        self,
        target: str = "localhost:50051",
        *,
        credentials: grpc.ChannelCredentials | None = None,
        metadata: _GrpcMetadata | None = None,
        _connect: ConnectClient | None = None,
    ) -> None:
        """Initialize the gRPC client.

        Parameters
        ----------
        target
            gRPC server address (e.g. ``"localhost:50051"`` or
            ``"sandbox.example.com:443"``).
        credentials
            Optional TLS/SSL channel credentials. If ``None``, uses insecure
            channel.
        metadata
            Optional default metadata to send with every RPC call.
        """
        self._target = target
        self._static_metadata = metadata
        self._connect = _connect
        if credentials is not None:
            self._channel = grpc.secure_channel(target, credentials)
        else:
            self._channel = grpc.insecure_channel(target)
        self._stub = pb_grpc.SandboxServiceStub(self._channel)
        self._process_stub = process_pb_grpc.SandboxProcessServiceStub(self._channel)

    @classmethod
    def from_env(cls, *, chalk_client: Any | None = None) -> SandboxClient:
        """Create a ``SandboxClient`` from ambient Chalk credentials."""

        connect = ConnectClient(chalk_client=chalk_client)
        connect._ensure_auth()

        host, use_tls = grpc_target_from_url(connect._api_server or "")

        return cls(
            host,
            credentials=grpc.ssl_channel_credentials() if use_tls else None,
            metadata=connect._grpc_metadata(),
            _connect=(
                connect
                if getattr(connect, "_uses_web_identity_token", lambda: False)() is True
                else None
            ),
        )

    @property
    def _metadata(self) -> _GrpcMetadata | None:
        if self._connect is not None:
            return self._connect._grpc_metadata()
        return self._static_metadata

    @_metadata.setter
    def _metadata(self, value: _GrpcMetadata | None) -> None:
        self._static_metadata = value

    def __enter__(self) -> SandboxClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the gRPC channel."""
        self._channel.close()

    def create(
        self,
        image: str | Image,
        *,
        name: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        env: dict[str, str] | None = None,
        volumes: _VolumeList | None = None,
        entrypoint: _StrList | None = None,
        network_policy: NetworkPolicy | None = None,
        restart_policy: RestartPolicy | None = None,
        chalk_identity: bool = False,
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
            Optional ``(volume_name, mount_path)`` pairs to mount into the
            sandbox.
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
            env=env,
            volumes=volumes,
            entrypoint=entrypoint,
            network_policy=network_policy,
            restart_policy=restart_policy,
            chalk_identity=chalk_identity,
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
        spec = container_pb.ChalkContainerSpec(image=image)
        if sandbox._name is not None:
            spec.name = sandbox._name
        if sandbox._cpu is not None or sandbox._memory is not None:
            limits = container_pb.ResourceLimits()
            if sandbox._cpu is not None:
                limits.cpu = sandbox._cpu
            if sandbox._memory is not None:
                limits.memory = sandbox._memory
            spec.resources.CopyFrom(limits)
        if sandbox._env:
            for k, v in sandbox._env.items():
                spec.env_vars[k] = v
        if sandbox._volumes:
            spec.volumes.extend(_volume_mounts_to_proto(sandbox._volumes))
        if sandbox._entrypoint is not None:
            spec.entrypoint.extend(sandbox._entrypoint)
        if sandbox._network_policy is not None:
            from google.protobuf.json_format import ParseDict

            ParseDict(sandbox._network_policy.to_dict(), spec.network_policy)
        if sandbox._restart_policy is not None:
            spec.restart_policy = (
                container_pb.RESTART_POLICY_NEVER
                if sandbox._restart_policy is RestartPolicy.NEVER
                else container_pb.RESTART_POLICY_ALWAYS
            )
        if sandbox._chalk_identity:
            spec.chalk_workload_identity.CopyFrom(container_pb.ChalkWorkloadIdentity())

        resp = self._stub.CreateSandbox(
            pb.CreateSandboxRequest(spec=spec), metadata=self._metadata
        )
        info = _pb_to_sandbox_info(resp.sandbox)

        if wait and info.status == "pending":
            info = self._poll_until_ready(
                info.id,
                poll_interval=poll_interval,
                timeout=timeout,
            )
        elif wait and info.status in ("failed", "error", "unknown"):
            raise _sandbox_creation_error(info)
        elif wait and info.status == "terminated":
            raise SandboxTerminatedError(
                f"Sandbox {info.id} was terminated during creation"
            )

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
                resp = self._stub.GetSandbox(
                    pb.GetSandboxRequest(id=sandbox_id),
                    metadata=self._metadata,
                )
            except grpc.RpcError as e:
                _handle_rpc_error(e, sandbox_id)
                raise
            info = _pb_to_sandbox_info(resp.sandbox)
            if info.status == "running":
                return info
            if info.status in ("failed", "error", "unknown"):
                raise _sandbox_creation_error(info)
            if info.status == "terminated":
                raise SandboxTerminatedError(
                    f"Sandbox {sandbox_id} was terminated during creation"
                )
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
        resp = self._stub.ListSandboxes(
            pb.ListSandboxesRequest(), metadata=self._metadata
        )
        return [_pb_to_sandbox_info(s) for s in resp.sandboxes]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pb_to_sandbox_info(info: pb.SandboxInfo) -> SandboxInfo:
    status = pb.SandboxStatus.Name(info.status).removeprefix("SANDBOX_STATUS_").lower()
    return SandboxInfo(
        id=info.id,
        status=status,
        created_at=info.created_at.ToJsonString(),
        name=info.name or None,
        status_message=(
            info.status_message if info.HasField("status_message") else None
        ),
    )


def _sandbox_creation_error(info: SandboxInfo) -> SandboxCreationError:
    """Build a detailed error from the backend's sandbox status."""
    message = f"Sandbox {info.id} entered {info.status} state during creation"
    if info.status_message:
        message = f"{message}: {info.status_message}"
    return SandboxCreationError(
        message,
        sandbox_id=info.id,
        status=info.status,
        status_message=info.status_message,
        build_id=info.build_id,
    )


def _response_to_event(response: process_pb.OpenSessionResponse) -> ExecEvent:
    msg_type = response.WhichOneof("event")
    if msg_type == "session_attached":
        return ExecEvent(session_id=response.session_attached.session_id)
    elif msg_type == "output_data":
        od = response.output_data
        text = od.data.decode("utf-8", errors="replace")
        if od.stream == process_pb.OutputData.STREAM_STDOUT:
            return ExecEvent(stdout=text)
        elif od.stream == process_pb.OutputData.STREAM_STDERR:
            return ExecEvent(stderr=text)
        return ExecEvent(stdout=text)
    elif msg_type == "process_exited":
        pe = response.process_exited
        return ExecEvent(
            exit_code=pe.exit_code if pe.HasField("exit_code") else None,
            exit_signal=pe.signal if pe.HasField("signal") else None,
        )
    elif msg_type == "process_timed_out":
        pe = response.process_timed_out
        return ExecEvent(
            exit_code=pe.exit_code if pe.HasField("exit_code") else None,
            exit_signal=pe.signal if pe.HasField("signal") else None,
            error_code="timed_out",
            error_message="Process timed out",
        )
    elif msg_type == "process_failed":
        return ExecEvent(
            error_code="process_failed",
            error_message=response.process_failed.message,
        )
    elif msg_type == "error":
        return ExecEvent(
            error_code=response.error.code,
            error_message=response.error.message,
        )
    return ExecEvent()


def _handle_rpc_error(e: grpc.RpcError, sandbox_id: str) -> None:
    raise_for_grpc_error(
        e,
        {
            grpc.StatusCode.NOT_FOUND: (
                SandboxNotFoundError,
                f"Sandbox {sandbox_id} not found",
            ),
            grpc.StatusCode.FAILED_PRECONDITION: (
                SandboxTerminatedError,
                f"Sandbox {sandbox_id} has terminated",
            ),
        },
    )


def _volume_mounts_to_proto(
    volumes: list[VolumeMount],
) -> list[container_pb.VolumeMount]:
    return [
        container_pb.VolumeMount(name=v.name, mount_path=v.mount_path, type=MOUNT_TYPE)
        for v in volumes
    ]


def connect(target: str = "localhost:50051", **kwargs) -> SandboxClient:
    """Create a new ``SandboxClient``.

    Parameters
    ----------
    target
        gRPC server address.
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
