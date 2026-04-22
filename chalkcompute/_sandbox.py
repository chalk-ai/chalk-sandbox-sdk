"""
Python SDK for the Sandbox gRPC Service.

Usage:
    from chalkcompute import SandboxClient

    client = SandboxClient("localhost:50051")

    # Create a sandbox
    sandbox = client.create(image="ubuntu:latest", name="my-sandbox")
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

import os
import queue
import signal
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import grpc

from chalkcompute._connect import ConnectClient, grpc_target_from_url, raise_for_grpc_error
from chalkcompute._gen.chalk.sandbox.v1 import service_pb2 as pb
from chalkcompute._gen.chalk.sandbox.v1 import service_pb2_grpc as pb_grpc
from chalkcompute._image import Image
from chalkcompute._tracing import traced

# Aliases defined before SandboxClient, which has a ``.list()`` method that
# would otherwise shadow the builtin ``list`` in class-scope name resolution.
_GrpcMetadata = list[tuple[str, str]]
_VolumeMountList = list[pb.VolumeMount]


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
    """Raised when a sandbox image build fails."""

    pass


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

    pid: int | None = None
    """PID of the process, if known."""

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

    stdout: str | None = None
    """Stdout text chunk, if this event carries stdout output."""

    stderr: str | None = None
    """Stderr text chunk, if this event carries stderr output."""

    pid: int | None = None
    """PID of the started process, set on the process-started event."""

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
        return self.pid is not None

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
class ExecProcess:
    """Handle to a running exec stream, allowing stdin writes and signal sends."""

    _request_queue: queue.Queue = field(repr=False)
    _response_iterator: Iterator[pb.ExecResponse] = field(repr=False)
    _sandbox_id: str = field(repr=False)

    pid: int | None = None
    """PID of the running process once the server reports it."""

    def write_stdin(self, data: str | bytes) -> None:
        """Send data to the process's stdin.

        Parameters
        ----------
        data
            Bytes or string to write to stdin. Strings are UTF-8 encoded.
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._request_queue.put(pb.ExecRequest(stdin_data=pb.StdinData(data=data)))

    def close_stdin(self) -> None:
        """Signal EOF on stdin."""
        self._request_queue.put(pb.ExecRequest(stdin_eof=pb.StdinEof()))

    def send_signal(self, sig: int = signal.SIGTERM) -> None:
        """Send a signal to the process.

        Parameters
        ----------
        sig
            Signal number to send. Defaults to ``SIGTERM``.
        """
        self._request_queue.put(pb.ExecRequest(signal=pb.ExecSignal(signal=sig)))

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
            if event.pid is not None:
                self.pid = event.pid
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
        pid: int | None = self.pid
        error: str | None = None

        for response in self._response_iterator:
            msg_type = response.WhichOneof("message")
            if msg_type == "process_started":
                pid = response.process_started.pid
            elif msg_type == "output_data":
                od = response.output_data
                if od.stream == pb.OutputData.STREAM_STDOUT:
                    stdout_parts.append(od.data)
                elif od.stream == pb.OutputData.STREAM_STDERR:
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
            pid=pid,
            error=error,
        )


# ---------------------------------------------------------------------------
# Sandbox handle
# ---------------------------------------------------------------------------


class Sandbox:
    """A handle to a sandbox instance.

    Obtain instances from :meth:`SandboxClient.create` or
    :meth:`SandboxClient.get`.  Use :meth:`exec` to run commands and
    :meth:`terminate` to shut the sandbox down when you're done.

    Examples
    --------
    >>> from chalkcompute import SandboxClient, Image
    >>> sandbox = SandboxClient().create(
    ...     image=Image.debian_slim().pip_install(["numpy"]),
    ... )
    >>> result = sandbox.exec(
    ...     "python",
    ...     "-c",
    ...     "import numpy; print(numpy.__version__)",
    ... )
    >>> result.stdout_text.strip()
    '1.26.4'
    >>> sandbox.terminate()
    """

    def __init__(self, client: SandboxClient, sandbox_id: str) -> None:
        self._client = client
        self._id = sandbox_id
        self._info: SandboxInfo | None = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def info(self) -> SandboxInfo:
        """Get sandbox info, fetching from server if needed."""
        if self._info is None:
            self._info = self._fetch_info()
        return self._info

    def refresh(self) -> SandboxInfo:
        """Force-refresh sandbox info from the server."""
        self._info = self._fetch_info()
        return self._info

    def _fetch_info(self) -> SandboxInfo:
        try:
            resp = self._client._stub.GetSandbox(
                pb.GetSandboxRequest(sandbox_id=self._id),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._id)
            raise
        return _pb_to_sandbox_info(resp.sandbox)

    @traced("chalkcompute.sandbox.exec")
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
        init = pb.ExecInit(
            sandbox_id=self._id,
            command=command,
            args=list(args),
        )
        if workdir is not None:
            init.workdir = workdir
        if env is not None:
            for k, v in env.items():
                init.env[k] = v
        if timeout_secs is not None:
            init.timeout_secs = timeout_secs

        request_queue: queue.Queue[pb.ExecRequest | None] = queue.Queue()
        # Send the init message first
        request_queue.put(pb.ExecRequest(init=init))

        def request_iterator() -> Iterator[pb.ExecRequest]:
            while True:
                req = request_queue.get()
                if req is None:
                    return
                yield req

        try:
            response_iterator = self._client._stub.Exec(
                request_iterator(), metadata=self._client._metadata
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._id)
            raise

        return ExecProcess(
            _request_queue=request_queue,
            _response_iterator=response_iterator,
            _sandbox_id=self._id,
        )

    @traced("chalkcompute.sandbox.terminate")
    def terminate(self, grace_period_seconds: int | None = None) -> None:
        """Terminate this sandbox.

        Parameters
        ----------
        grace_period_seconds
            Optional grace period to allow running processes to shut down.
        """
        req = pb.TerminateSandboxRequest(sandbox_id=self._id)
        if grace_period_seconds is not None:
            req.grace_period_seconds = grace_period_seconds
        try:
            self._client._stub.TerminateSandbox(req, metadata=self._client._metadata)
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._id)
            raise


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


_SandboxInfoList = list[SandboxInfo]


class SandboxClient:
    """Client for the Sandbox gRPC Service."""

    def __init__(
        self,
        target: str = "localhost:50051",
        *,
        credentials: grpc.ChannelCredentials | None = None,
        metadata: _GrpcMetadata | None = None,
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
        self._metadata = metadata
        if credentials is not None:
            self._channel = grpc.secure_channel(target, credentials)
        else:
            self._channel = grpc.insecure_channel(target)
        self._stub = pb_grpc.SandboxServiceStub(self._channel)

    @classmethod
    def from_env(cls) -> SandboxClient:
        """Create a ``SandboxClient`` from standard ``CHALK_*`` environment vars."""
        if os.environ.get("CHALK_API_SERVER") is None:
            raise ValueError(
                "CHALK_API_SERVER must be set to construct SandboxClient from env"
            )

        connect = ConnectClient()
        connect._ensure_auth()

        host, _use_tls = grpc_target_from_url(connect._api_server or "")

        return cls(
            host,
            credentials=grpc.ssl_channel_credentials(),
            metadata=[
                ("authorization", f"Bearer {connect._access_token}"),
                ("x-chalk-env-id", connect._env_id or ""),
                ("x-chalk-server", "go-api"),
            ],
        )

    def __enter__(self) -> SandboxClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the gRPC channel."""
        self._channel.close()

    @traced("chalkcompute.sandbox.create")
    def create(
        self,
        image: str | Image,
        *,
        name: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        env: dict[str, str] | None = None,
        volumes: _VolumeMountList | None = None,
        wait: bool = True,
        poll_interval: float = 0.5,
        timeout: float = 600.0,
    ) -> Sandbox:
        """Create a new sandbox.

        When ``image`` is an ``Image`` spec, the server builds it asynchronously
        and returns a sandbox in the "building" state.  If ``wait`` is ``True``
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
            Optional list of ``VolumeMount`` protos to attach.
        wait
            If ``True``, poll until the sandbox is ready (default ``True``).
        poll_interval
            Seconds between ``GetSandbox`` polls (default 5).
        timeout
            Maximum seconds to wait for the sandbox to become ready
            (default 600).

        Returns
        -------
        Sandbox
            A sandbox handle.
        """
        req = pb.CreateSandboxRequest()
        if isinstance(image, Image):
            req.image_spec.CopyFrom(image.to_proto())
        else:
            req.image = image
        if name is not None:
            req.name = name
        if cpu is not None or memory is not None:
            limits = pb.ResourceLimits()
            if cpu is not None:
                limits.cpu = cpu
            if memory is not None:
                limits.memory = memory
            req.resource_limits.CopyFrom(limits)
        if env is not None:
            for k, v in env.items():
                req.env[k] = v
        if volumes is not None:
            req.volumes.extend(volumes)

        resp = self._stub.CreateSandbox(req, metadata=self._metadata)
        info = _pb_to_sandbox_info(resp.sandbox)
        sandbox = Sandbox(self, info.id)
        sandbox._info = info

        if wait and info.status in ("building", "connecting"):
            sandbox._info = self._poll_until_ready(
                sandbox.id,
                poll_interval=poll_interval,
                timeout=timeout,
            )

        return sandbox

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
                    pb.GetSandboxRequest(sandbox_id=sandbox_id),
                    metadata=self._metadata,
                )
            except grpc.RpcError as e:
                _handle_rpc_error(e, sandbox_id)
                raise
            info = _pb_to_sandbox_info(resp.sandbox)
            if info.status == "ready":
                return info
            if info.status == "error":
                raise SandboxBuildError(
                    f"Sandbox {sandbox_id} entered error state during image build"
                )
            if info.status == "terminated":
                raise SandboxTerminatedError(
                    f"Sandbox {sandbox_id} was terminated during image build"
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
        return Sandbox(self, id)

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
    return SandboxInfo(
        id=info.id,
        status=info.state,
        created_at=info.created_at,
        name=info.name if info.HasField("name") else None,
        build_id=info.build_id if info.HasField("build_id") else None,
    )


def _response_to_event(response: pb.ExecResponse) -> ExecEvent:
    msg_type = response.WhichOneof("message")
    if msg_type == "process_started":
        return ExecEvent(pid=response.process_started.pid)
    elif msg_type == "output_data":
        od = response.output_data
        text = od.data.decode("utf-8", errors="replace")
        if od.stream == pb.OutputData.STREAM_STDOUT:
            return ExecEvent(stdout=text)
        elif od.stream == pb.OutputData.STREAM_STDERR:
            return ExecEvent(stderr=text)
        return ExecEvent(stdout=text)
    elif msg_type == "process_exited":
        pe = response.process_exited
        return ExecEvent(
            exit_code=pe.exit_code if pe.HasField("exit_code") else None,
            exit_signal=pe.signal if pe.HasField("signal") else None,
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
        print(f"  - {sandbox_info.id} ({sandbox_info.status}) {sandbox_info.name or ''}")

    sandboxes = client.list()
    if sandboxes:
        sandbox = client.get(id=sandboxes[0].id)
        print(f"\nExecuting 'echo hello' on sandbox {sandbox.id}:")
        result = sandbox.exec("echo", "hello")
        print(f"  stdout: {result.stdout_text.rstrip()}")
        print(f"  exit_code: {result.exit_code}")
