"""File transfer operations for running sandboxes."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from connectrpc.errors import ConnectError
from protobuf import Oneof

from chalkcompute._gen.chalk.sandbox.v2 import filesystem_service_pb as fs_pb
from chalkcompute._sandbox import Sandbox, SandboxError
from chalkcompute._tracing import _instrumented

if TYPE_CHECKING:
    from chalkcompute._gen.chalk.sandbox.v2.filesystem_service_pb import (
        UploadFileRequest,
    )


FilesystemError = fs_pb.FilesystemError

_CHUNK_SIZE = 256 * 1024
_ERROR_DETAIL_TYPE = "chalk.sandbox.v2.FilesystemErrorDetail"


class SandboxFileSystemError(SandboxError):
    """A filesystem operation rejected by the sandbox service."""

    def __init__(
        self,
        message: str,
        *,
        error: FilesystemError,
        errno: int | None = None,
    ) -> None:
        self.error = error
        self.errno = errno
        super().__init__(message)


def _path(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("path must be a nonempty string without NUL bytes")
    return value


def _mode(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("mode must be an integer or None")
    if not 0 <= value <= 0o7777:
        raise ValueError("mode must be between 0 and 0o7777")
    return value


def _timeout_ms(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("timeout_secs must be an integer or None")
    if value <= 0:
        raise ValueError("timeout_secs must be greater than zero")
    return value * 1000


def _rpc_error(error: ConnectError) -> SandboxFileSystemError:
    filesystem_error = FilesystemError.UNSPECIFIED
    errno = None
    for detail in error.details:
        if detail.type_name != _ERROR_DETAIL_TYPE:
            continue
        try:
            value = detail.value(fs_pb.FilesystemErrorDetail)
        except ValueError:
            continue
        if value is not None:
            filesystem_error = value.error
            errno = value.errno if value.has_field("errno") else None
            break
    return SandboxFileSystemError(
        error.message,
        error=filesystem_error,
        errno=errno,
    )


class SandboxFileSystem:
    """Transfer files to and from a running sandbox."""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def _upload(
        self,
        chunks: Iterable[bytes],
        path: str,
        *,
        mode: int | None,
        timeout_secs: int | None,
    ) -> None:
        destination = _path(path)
        file_mode = _mode(mode)
        timeout_ms = _timeout_ms(timeout_secs)
        client = self._sandbox._require_client()
        sandbox_id = self._sandbox._require_started()
        bytes_written = 0

        def requests() -> Iterator[UploadFileRequest]:
            nonlocal bytes_written
            yield fs_pb.UploadFileRequest(
                payload=Oneof(
                    "metadata",
                    fs_pb.UploadFileMetadata(
                        sandbox_id=sandbox_id,
                        destination=destination,
                        mode=file_mode,
                    ),
                )
            )

            pending = bytearray()
            for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("upload chunks must be bytes")
                view = memoryview(chunk)
                offset = 0
                while offset < len(view):
                    size = min(_CHUNK_SIZE - len(pending), len(view) - offset)
                    pending.extend(view[offset : offset + size])
                    offset += size
                    if len(pending) == _CHUNK_SIZE:
                        data = bytes(pending)
                        bytes_written += len(data)
                        yield fs_pb.UploadFileRequest(payload=Oneof("data", data))
                        pending.clear()

            if pending:
                data = bytes(pending)
                bytes_written += len(data)
                yield fs_pb.UploadFileRequest(payload=Oneof("data", data))

        try:
            response = client._filesystem_stub.upload_file(
                requests(),
                headers=client._headers,
                timeout_ms=timeout_ms,
            )
        except ConnectError as error:
            raise _rpc_error(error) from error

        if response.bytes_written != bytes_written:
            raise SandboxFileSystemError(
                "Sandbox reported an unexpected upload byte count",
                error=FilesystemError.INTERNAL,
            )

    def _download(
        self,
        path: str,
        *,
        timeout_secs: int | None,
    ) -> Iterator[bytes]:
        source = _path(path)
        timeout_ms = _timeout_ms(timeout_secs)
        client = self._sandbox._require_client()
        request = fs_pb.DownloadFileRequest(
            sandbox_id=self._sandbox._require_started(),
            path=source,
        )
        responses = None
        try:
            responses = client._filesystem_stub.download_file(
                request,
                headers=client._headers,
                timeout_ms=timeout_ms,
            )
            for response in responses:
                yield response.data
        except ConnectError as error:
            raise _rpc_error(error) from error
        finally:
            if responses is not None:
                close = getattr(responses, "close", None)
                if close is not None:
                    close()

    @_instrumented("chalkcompute.sandbox.fs.write_bytes")
    def write_bytes(
        self,
        path: str,
        data: bytes,
        *,
        mode: int | None = None,
        timeout_secs: int | None = None,
    ) -> None:
        """Write ``data`` to ``path`` in the sandbox."""
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        self._upload((data,), path, mode=mode, timeout_secs=timeout_secs)

    @_instrumented("chalkcompute.sandbox.fs.upload_iter")
    def upload_iter(
        self,
        chunks: Iterable[bytes],
        path: str,
        *,
        mode: int | None = None,
        timeout_secs: int | None = None,
    ) -> None:
        """Upload an iterable of byte chunks to ``path`` in the sandbox."""
        self._upload(chunks, path, mode=mode, timeout_secs=timeout_secs)

    @_instrumented("chalkcompute.sandbox.fs.upload_file")
    def upload_file(
        self,
        local_path: str | Path,
        path: str,
        *,
        mode: int | None = None,
        timeout_secs: int | None = None,
    ) -> None:
        """Upload a local file to ``path`` in the sandbox."""
        with Path(local_path).open("rb") as source:
            self._upload(
                iter(lambda: source.read(_CHUNK_SIZE), b""),
                path,
                mode=mode,
                timeout_secs=timeout_secs,
            )

    @_instrumented("chalkcompute.sandbox.fs.read_bytes")
    def read_bytes(
        self,
        path: str,
        *,
        timeout_secs: int | None = None,
    ) -> bytes:
        """Read an entire sandbox file into memory."""
        return b"".join(self._download(path, timeout_secs=timeout_secs))

    @_instrumented("chalkcompute.sandbox.fs.download_iter")
    def download_iter(
        self,
        path: str,
        *,
        timeout_secs: int | None = None,
    ) -> Iterator[bytes]:
        """Yield the contents of a sandbox file as byte chunks."""
        yield from self._download(path, timeout_secs=timeout_secs)

    @_instrumented("chalkcompute.sandbox.fs.download_file")
    def download_file(
        self,
        path: str,
        local_path: str | Path,
        *,
        timeout_secs: int | None = None,
    ) -> None:
        """Download a sandbox file, atomically replacing ``local_path``."""
        destination = Path(local_path)
        fd, temporary_path = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        try:
            with os.fdopen(fd, "wb") as destination_file:
                for chunk in self._download(path, timeout_secs=timeout_secs):
                    destination_file.write(chunk)
            os.replace(temporary_path, destination)
        finally:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
