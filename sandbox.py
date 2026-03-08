#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx", "websockets"]
# ///
"""
Simple Python client for the Sandbox Control Plane.

Usage:
    from sandbox import SandboxClient

    client = SandboxClient("http://localhost:8080")
    sandbox = client.get(id="550e8400-e29b-41d4-a716-446655440000")

    # Execute a command and stream output
    process = sandbox.exec("python", "-c", "print('hello')")
    for line in process.stdout:
        print(line)

    # Check exit code
    print(f"Exit code: {process.exit_code}")

    # Stream stdout in real-time via WebSocket (async)
    async for data in sandbox.stdout.listen():
        print(data)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Iterator

import httpx


class SandboxError(Exception):
    """Base exception for sandbox errors."""
    pass


class SandboxNotFoundError(SandboxError):
    """Raised when a sandbox is not found."""
    pass


class SandboxTerminatedError(SandboxError):
    """Raised when a sandbox has terminated."""
    pass


class ExecError(SandboxError):
    """Raised when command execution fails."""
    pass


@dataclass
class SandboxInfo:
    """Information about a sandbox."""
    id: str
    state: str
    connected_at: str
    last_activity: str
    remote_addr: str


@dataclass
class ExecResult:
    """Result of a command execution."""
    _stdout: str
    _stderr: str
    exit_code: int | None
    error: str | None

    @property
    def stdout(self) -> Iterator[str]:
        """Iterate over stdout lines."""
        for line in self._stdout.splitlines():
            yield line

    @property
    def stderr(self) -> Iterator[str]:
        """Iterate over stderr lines."""
        for line in self._stderr.splitlines():
            yield line

    @property
    def stdout_text(self) -> str:
        """Get full stdout as text."""
        return self._stdout

    @property
    def stderr_text(self) -> str:
        """Get full stderr as text."""
        return self._stderr


class StdoutStream:
    """Handle to a sandbox's stdout WebSocket stream."""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def _get_ws_url(self) -> str:
        """Convert HTTP URL to WebSocket URL."""
        base = self._sandbox._client._base_url
        ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
        return f"{ws_base}/v1/sandboxes/{self._sandbox._id}/stdout"

    async def listen(self) -> AsyncIterator[str]:
        """
        Subscribe to stdout output via WebSocket.

        Yields chunks of stdout data as they arrive from the sandbox.
        The connection stays open until the sandbox closes or disconnects.

        Usage:
            async for data in sandbox.stdout.listen():
                print(data)
        """
        import websockets

        url = self._get_ws_url()
        async with websockets.connect(url) as ws:
            async for message in ws:
                if isinstance(message, bytes):
                    yield message.decode("utf-8")
                else:
                    yield message


class Sandbox:
    """A handle to a sandbox instance."""

    def __init__(self, client: SandboxClient, sandbox_id: str) -> None:
        self._client = client
        self._id = sandbox_id
        self._info: SandboxInfo | None = None
        self._stdout: StdoutStream | None = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def stdout(self) -> StdoutStream:
        """Access the stdout stream for real-time output."""
        if self._stdout is None:
            self._stdout = StdoutStream(self)
        return self._stdout

    @property
    def info(self) -> SandboxInfo:
        """Get sandbox info, fetching if needed."""
        if self._info is None:
            self._info = self._fetch_info()
        return self._info

    def _fetch_info(self) -> SandboxInfo:
        """Fetch sandbox information from the server."""
        response = self._client._http.get(f"/v1/sandboxes/{self._id}")
        if response.status_code == 404:
            raise SandboxNotFoundError(f"Sandbox {self._id} not found")
        response.raise_for_status()
        data = response.json()
        return SandboxInfo(
            id=data["id"],
            state=data["state"],
            connected_at=data["connected_at"],
            last_activity=data["last_activity"],
            remote_addr=data["remote_addr"],
        )

    def exec(
        self,
        command: str,
        *args: str,
        timeout_sec: int | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """
        Execute a command in the sandbox.

        Args:
            command: The command to execute
            *args: Command arguments
            timeout_sec: Optional timeout in seconds
            workdir: Optional working directory
            env: Optional environment variables

        Returns:
            ExecResult with stdout/stderr iterators and exit_code

        Raises:
            SandboxNotFoundError: If the sandbox doesn't exist
            SandboxTerminatedError: If the sandbox has terminated
            ExecError: If execution fails
        """
        payload: dict = {
            "command": command,
            "args": list(args),
        }
        if timeout_sec is not None:
            payload["timeout_sec"] = timeout_sec
        if workdir is not None:
            payload["workdir"] = workdir
        if env is not None:
            payload["env"] = env

        # Set a generous client timeout (server handles process timeout)
        request_timeout = (timeout_sec or 60) + 10

        response = self._client._http.post(
            f"/v1/sandboxes/{self._id}/exec",
            json=payload,
            timeout=request_timeout,
        )

        if response.status_code == 404:
            raise SandboxNotFoundError(f"Sandbox {self._id} not found")
        if response.status_code == 410:
            raise SandboxTerminatedError(f"Sandbox {self._id} has terminated")

        data = response.json()

        if response.status_code >= 400:
            raise ExecError(data.get("error", f"Execution failed: {response.status_code}"))

        return ExecResult(
            _stdout=data.get("stdout", ""),
            _stderr=data.get("stderr", ""),
            exit_code=data.get("exit_code"),
            error=data.get("error"),
        )


class SandboxClient:
    """Client for the Sandbox Control Plane API."""

    def __init__(self, base_url: str = "http://localhost:8080") -> None:
        """
        Initialize the client.

        Args:
            base_url: Base URL of the control plane API
        """
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=base_url)

    def __enter__(self) -> SandboxClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()

    def health(self) -> dict:
        """Check the health of the control plane."""
        response = self._http.get("/health")
        response.raise_for_status()
        return response.json()

    def list(self) -> list[SandboxInfo]:
        """List all sandboxes."""
        response = self._http.get("/v1/sandboxes/")
        response.raise_for_status()
        data = response.json()
        return [
            SandboxInfo(
                id=s["id"],
                state=s["state"],
                connected_at=s["connected_at"],
                last_activity=s["last_activity"],
                remote_addr=s["remote_addr"],
            )
            for s in data.get("sandboxes", [])
        ]

    def get(self, id: str) -> Sandbox:
        """
        Get a sandbox handle by ID.

        Note: This does not validate that the sandbox exists until
        you call a method on it (like exec or info).

        Args:
            id: The sandbox ID

        Returns:
            A Sandbox handle
        """
        return Sandbox(self, id)


# Convenience function for simple usage
def connect(base_url: str = "http://localhost:8080") -> SandboxClient:
    """Create a new SandboxClient."""
    return SandboxClient(base_url)


if __name__ == "__main__":
    # Example usage
    client = SandboxClient()

    print("Health check:")
    print(client.health())

    print("\nListing sandboxes:")
    for sandbox_info in client.list():
        print(f"  - {sandbox_info.id} ({sandbox_info.state})")

    # If there's a sandbox available, run a command
    sandboxes = client.list()
    if sandboxes:
        sandbox = client.get(id=sandboxes[0].id)
        print(f"\nExecuting 'echo hello' on sandbox {sandbox.id}:")
        result = sandbox.exec("echo", "hello")
        for line in result.stdout:
            print(f"  stdout: {line}")
        print(f"  exit_code: {result.exit_code}")
