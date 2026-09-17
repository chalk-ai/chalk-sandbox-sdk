"""Filesystem helpers backed by the runtime's native sandbox entrypoint."""

from __future__ import annotations

import errno
import io
import json
import os
import struct
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, overload

if TYPE_CHECKING:
    from chalkcompute._sandbox import ExecProcess, Sandbox

_ENTRYPOINT = "/.chalk/bin/chalk-sandbox-entrypoint"
_MAX_FRAME = 1024 * 1024
_CHUNK_SIZE = 64 * 1024


class SandboxFileSystemError(OSError):
    """Filesystem or transport failure; errno is retained when available."""


@dataclass(frozen=True)
class SandboxFileInfo:
    """lstat metadata for a sandbox path; permissions are an octal string."""

    name: str
    path: str
    size: int
    is_dir: bool
    is_symlink: bool
    permissions: str
    modified_time: float


@dataclass(frozen=True)
class FileMatch:
    """One line containing literal search text; line numbers are one-based."""

    path: str
    line_number: int
    line: str


def _path(path: str) -> str:
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("path must be a nonempty string without NUL bytes")
    return path


def _mode(mode: str) -> int:
    if (
        not isinstance(mode, str)
        or not 3 <= len(mode) <= 4
        or any(c not in "01234567" for c in mode)
    ):
        raise ValueError("mode must be a three- or four-digit octal string")
    return int(mode, 8)


def _protocol_error(message: str) -> SandboxFileSystemError:
    return SandboxFileSystemError(errno.EPROTO, message)


class _Session:
    def __init__(self, process: ExecProcess) -> None:
        self.process = process
        self.stdout = process.stdout_bytes()
        self.buffer = bytearray()
        self.chunk_size = _CHUNK_SIZE
        self.closed = False

    def read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            try:
                self.buffer.extend(next(self.stdout))
            except StopIteration:
                raise _protocol_error(
                    "Filesystem entrypoint closed before its response completed"
                ) from None
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def request(self, op: str, *, data: bytes = b"", **fields: Any) -> Any:
        payload = json.dumps({"op": op, **fields}, ensure_ascii=True).encode()
        if len(payload) > _MAX_FRAME:
            raise ValueError("filesystem request exceeds 1 MiB")
        self.process.write_stdin(struct.pack(">I", len(payload)) + payload + data)
        size = struct.unpack(">I", self.read_exact(4))[0]
        if not 0 < size <= _MAX_FRAME:
            raise _protocol_error("Invalid filesystem response frame size")
        try:
            response = json.loads(self.read_exact(size))
        except (ValueError, UnicodeError) as exc:
            raise _protocol_error("Invalid filesystem response JSON") from exc
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise _protocol_error("Invalid filesystem response envelope")
        if not response["ok"]:
            code = response.get("errno")
            message = response.get("message")
            if type(code) is not int or not isinstance(message, str):
                raise _protocol_error("Invalid filesystem error response")
            raise SandboxFileSystemError(code, message)
        if "value" not in response:
            raise _protocol_error("Filesystem response is missing its value")
        return response["value"]

    def negotiate(self) -> None:
        hello = self.request("hello", version=1)
        if (
            not isinstance(hello, dict)
            or type(hello.get("version")) is not int
            or hello["version"] != 1
        ):
            raise _protocol_error(
                "Unsupported filesystem entrypoint protocol; expected version 1"
            )
        size = hello.get("chunk_size")
        if type(size) is not int or not 0 < size <= _CHUNK_SIZE:
            raise _protocol_error("Invalid filesystem transfer chunk size")
        self.chunk_size = size

    def read(self) -> bytes:
        result = self.request("read")
        size = result.get("size") if isinstance(result, dict) else None
        if type(size) is not int or not 0 <= size <= self.chunk_size:
            raise _protocol_error("Invalid filesystem read size")
        return self.read_exact(size)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.process.close_stdin()
            # EOF makes the helper close its handles and discard uncommitted uploads.
            for _ in self.stdout:
                pass
        finally:
            close = getattr(self.stdout, "close", None)
            if close is not None:
                close()


class SandboxFileSystem:
    """Access files through ``sandbox.fs`` using the runtime-provided entrypoint.

    Requires a runtime that provides ``/.chalk/bin/chalk-sandbox-entrypoint``.
    Paths are relative to the image working directory. No image dependencies or
    extra authentication are needed. Transfers are bounded; whole-file writes
    publish atomically on commit. Failed streams are never replayed.
    """

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    @contextmanager
    def _session(self, *, timeout_secs: int | None = None) -> Iterator[_Session]:
        from chalkcompute._sandbox import SandboxError
        from connectrpc.errors import ConnectError

        process = self._sandbox.exec_start(
            _ENTRYPOINT, "fs", "--protocol=1", timeout_secs=timeout_secs
        )
        session = _Session(process)
        try:
            try:
                session.negotiate()
            except (SandboxError, ConnectError, OSError) as exc:
                raise SandboxFileSystemError(
                    getattr(exc, "errno", None) or errno.EIO,
                    f"Cannot start the sandbox filesystem entrypoint: {exc}. "
                    "This requires a supporting runtime and a newly created sandbox.",
                ) from exc
            try:
                yield session
                session.close()
            except (SandboxError, ConnectError) as exc:
                raise SandboxFileSystemError(
                    errno.EIO, f"Filesystem exec failed: {exc}"
                ) from exc
        except BaseException:
            with suppress(Exception):
                session.close()
            raise

    def get_work_dir(self) -> str:
        """Return the default working directory used by sandbox commands."""
        with self._session() as session:
            return str(session.request("work_dir"))

    def get_user_home_dir(self) -> str:
        """Return the execution user's home from the sandbox account database."""
        with self._session() as session:
            return str(session.request("home_dir"))

    def download_file_stream(
        self, remote_path: str, *, timeout_secs: int | None = None
    ) -> Iterator[bytes]:
        """Yield bounded binary chunks. Close on early exit; discard data on error."""
        _path(remote_path)
        with self._session(timeout_secs=timeout_secs) as session:
            session.request("read_open", path=remote_path)
            while chunk := session.read():
                yield chunk

    def read_file(self, path: str, *, timeout_secs: int | None = None) -> bytes:
        """Read the entire file into memory as bytes."""
        return b"".join(self.download_file_stream(path, timeout_secs=timeout_secs))

    def read_text(
        self, path: str, *, encoding: str = "utf-8", timeout_secs: int | None = None
    ) -> str:
        """Decode a file as text, rejecting invalid encoding."""
        return self.read_file(path, timeout_secs=timeout_secs).decode(encoding)

    def upload_file_stream(
        self,
        chunks: Iterable[bytes],
        destination: str,
        *,
        timeout_secs: int | None = None,
    ) -> None:
        """Stream to an adjacent temporary file, then atomically publish it.

        Parent directories must exist. Preserve existing permissions; new files
        use 0644. Reject symlink destinations. A lost commit response has an
        unknown outcome; no operation is automatically retried.
        """
        _path(destination)
        with self._session(timeout_secs=timeout_secs) as session:
            session.request("write_begin", path=destination)
            for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("upload chunks must be bytes")
                for offset in range(0, len(chunk), session.chunk_size):
                    data = chunk[offset : offset + session.chunk_size]
                    session.request("write", size=len(data), data=data)
            session.request("write_commit")

    def write_file(
        self, path: str, data: str | bytes, *, timeout_secs: int | None = None
    ) -> None:
        """Atomically replace a file with bytes or UTF-8 text."""
        if isinstance(data, str):
            data = data.encode()
        if not isinstance(data, bytes):
            raise TypeError("data must be str or bytes")
        self.upload_file_stream([data], path, timeout_secs=timeout_secs)

    def upload_file(
        self,
        source: str | Path | bytes,
        destination: str,
        *,
        timeout_secs: int | None = None,
    ) -> None:
        """Upload a local file in bounded chunks, or upload supplied bytes."""
        _path(destination)
        with (
            io.BytesIO(source)
            if isinstance(source, bytes)
            else open(source, "rb") as file
        ):
            self.upload_file_stream(
                iter(lambda: file.read(_CHUNK_SIZE), b""),
                destination,
                timeout_secs=timeout_secs,
            )

    @overload
    def download_file(
        self,
        remote_path: str,
        local_path: None = None,
        *,
        timeout_secs: int | None = None,
    ) -> bytes: ...

    @overload
    def download_file(
        self,
        remote_path: str,
        local_path: str | Path,
        *,
        timeout_secs: int | None = None,
    ) -> None: ...

    def download_file(
        self,
        remote_path: str,
        local_path: str | Path | None = None,
        *,
        timeout_secs: int | None = None,
    ) -> bytes | None:
        """Return bytes, or stream to a local path and replace it atomically."""
        _path(remote_path)
        if local_path is None:
            return self.read_file(remote_path, timeout_secs=timeout_secs)
        destination = Path(local_path)
        fd, temporary = tempfile.mkstemp(
            prefix=".chalk-download-", dir=destination.parent
        )
        try:
            with os.fdopen(fd, "wb") as file:
                for chunk in self.download_file_stream(
                    remote_path, timeout_secs=timeout_secs
                ):
                    file.write(chunk)
            os.replace(temporary, destination)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    def get_file_info(
        self, path: str, *, timeout_secs: int | None = None
    ) -> SandboxFileInfo:
        """Return metadata without following a final symbolic link."""
        _path(path)
        with self._session(timeout_secs=timeout_secs) as session:
            return SandboxFileInfo(**session.request("stat", path=path))

    def list_files(
        self, path: str = ".", *, timeout_secs: int | None = None
    ) -> list[SandboxFileInfo]:
        """Return directory entries sorted by name, excluding dot and dot-dot."""
        _path(path)
        with self._session(timeout_secs=timeout_secs) as session:
            session.request("list_open", path=path)
            entries = []
            while page := session.request("list_next"):
                entries.extend(SandboxFileInfo(**entry) for entry in page)
            return sorted(entries, key=lambda entry: entry.name)

    def create_folder(
        self, path: str, mode: str = "755", *, timeout_secs: int | None = None
    ) -> None:
        """Create missing parent directories with the requested mode, subject to umask."""
        _path(path)
        permissions = _mode(mode)
        with self._session(timeout_secs=timeout_secs) as session:
            session.request("mkdir", path=path, mode=permissions)

    def delete_file(
        self, path: str, *, recursive: bool = False, timeout_secs: int | None = None
    ) -> None:
        """Remove a path; directories require recursion, which never follows symlinks."""
        _path(path)
        if not isinstance(recursive, bool):
            raise TypeError("recursive must be bool")
        with self._session(timeout_secs=timeout_secs) as session:
            session.request("remove", path=path, recursive=recursive)

    def move_files(
        self, source: str, destination: str, *, timeout_secs: int | None = None
    ) -> None:
        """Rename a path, replacing the destination according to OS semantics."""
        _path(source)
        _path(destination)
        with self._session(timeout_secs=timeout_secs) as session:
            session.request("rename", source=source, destination=destination)

    def set_file_permissions(
        self,
        path: str,
        *,
        mode: str | None = None,
        owner: int | None = None,
        group: int | None = None,
        timeout_secs: int | None = None,
    ) -> None:
        """Set permissions or numeric UID/GID using the execution user's privileges."""
        _path(path)
        permissions = _mode(mode) if mode is not None else None
        for value in (owner, group):
            if value is not None and (
                type(value) is not int or not 0 <= value < 2**32 - 1
            ):
                raise ValueError(
                    "owner and group must be nonnegative numeric UID/GID values"
                )
        if mode is None and owner is None and group is None:
            raise ValueError("provide mode, owner, or group")
        with self._session(timeout_secs=timeout_secs) as session:
            session.request(
                "permissions", path=path, mode=permissions, uid=owner, gid=group
            )

    def _walk(self, path: str, timeout_secs: int | None) -> Iterator[SandboxFileInfo]:
        pending = [path]
        while pending:
            for entry in self.list_files(pending.pop(), timeout_secs=timeout_secs):
                if entry.is_symlink:
                    continue
                if entry.is_dir:
                    pending.append(entry.path)
                else:
                    yield entry

    def search_files(
        self, path: str, pattern: str, *, timeout_secs: int | None = None
    ) -> list[str]:
        """Recursively match filename globs, skipping symbolic links."""
        import fnmatch

        _path(path)
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("pattern must be a nonempty string")
        return sorted(
            entry.path
            for entry in self._walk(path, timeout_secs)
            if fnmatch.fnmatchcase(entry.name, pattern)
        )

    def find_files(
        self, path: str, text: str, *, timeout_secs: int | None = None
    ) -> list[FileMatch]:
        """Find literal text in UTF-8 files, buffering one candidate file at a time."""
        _path(path)
        if not isinstance(text, str) or not text:
            raise ValueError("text must be a nonempty string")
        matches = []
        for entry in self._walk(path, timeout_secs):
            try:
                contents = self.read_text(entry.path, timeout_secs=timeout_secs)
            except UnicodeDecodeError:
                continue
            matches.extend(
                FileMatch(entry.path, number, line)
                for number, line in enumerate(contents.splitlines(), 1)
                if text in line
            )
        return sorted(matches, key=lambda match: (match.path, match.line_number))

    def replace_in_files(
        self,
        files: Iterable[str],
        old: str,
        new: str,
        *,
        timeout_secs: int | None = None,
    ) -> dict[str, int]:
        """Replace literal UTF-8 text, atomically per file; coordinate concurrent edits."""
        if isinstance(files, (str, bytes)):
            raise TypeError("files must be an iterable of paths")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError("old must be nonempty text and new must be text")
        result = {}
        for path in files:
            contents = self.read_text(_path(path), timeout_secs=timeout_secs)
            count = contents.count(old)
            if count:
                self.write_file(
                    path, contents.replace(old, new), timeout_secs=timeout_secs
                )
            result[path] = count
        return result
