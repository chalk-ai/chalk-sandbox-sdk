"""Native SFTP filesystem operations over the authenticated sandbox exec stream."""

from __future__ import annotations

import errno
import fnmatch
import io
import os
import posixpath
import stat
import tempfile
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, overload

from paramiko import SFTPAttributes, SFTPClient, SSHException

if TYPE_CHECKING:
    from chalkcompute._sandbox import ExecProcess, Sandbox

_CHUNK_SIZE = 32 * 1024
_DEFAULT_SERVER = "/usr/lib/openssh/sftp-server"


class SandboxFileSystemError(OSError):
    """Remote filesystem/transport failure, with errno when the server supplies it."""


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


class _ExecSocket:
    """Socket-like SFTP channel; exec already supplies authentication/encryption.

    SFTP writes wait for acknowledgments, bounding pending stdin as well as reads.
    No SSH daemon, listening port, SSH keys, or remote interpreter is involved.
    """

    def __init__(self, process: ExecProcess) -> None:
        self._process = process
        self._stdout = process.stdout_bytes()
        self._buffer = bytearray()
        self._closed = False

    def send(self, data: bytes) -> int:
        self._process.write_stdin(data)
        return len(data)

    def get_name(self) -> str:
        return "sandbox-exec"

    def recv(self, size: int) -> bytes:
        while not self._buffer:
            try:
                chunk = next(self._stdout)
            except StopIteration:
                return b""
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._process.close_stdin()
        try:
            # The native server exits on EOF. Drain to close its exec session.
            for _ in self._stdout:
                pass
        finally:
            close = getattr(self._stdout, "close", None)
            if close is not None:
                close()


class SandboxFileSystem:
    """Access via ``sandbox.fs``. Paths use the image's working directory.

    Uses the native OpenSSH SFTP server over Chalk's existing authenticated exec
    transport. The default image includes it; custom Debian/Ubuntu images can
    use ``.apt_install(["openssh-sftp-server"])``. For other layouts construct
    ``SandboxFileSystem(sandbox, server_path="/path/to/sftp-server")``.

    Uploads and downloads to a local path stream in bounded chunks. Convenience
    byte/text reads buffer the returned contents. Whole-file writes are atomic:
    an adjacent temporary file is renamed only after every write is acknowledged.
    Transport failures propagate; operations are never automatically restarted.
    """

    def __init__(self, sandbox: Sandbox, *, server_path: str = _DEFAULT_SERVER) -> None:
        self._sandbox = sandbox
        self._server_path = _path(server_path)

    @contextmanager
    def _session(
        self, *, timeout_secs: int | None = None, home: bool = False
    ) -> Iterator[SFTPClient]:
        # Import here to avoid a cycle with Sandbox.fs.
        from chalkcompute._sandbox import SandboxError
        from connectrpc.errors import ConnectError

        process = self._sandbox.exec_start(
            self._server_path,
            *(["-d", "%d"] if home else []),
            timeout_secs=timeout_secs,
        )
        channel = _ExecSocket(process)
        client = None
        try:
            client = SFTPClient(channel)
            yield client
        except (OSError, EOFError, SSHException, SandboxError, ConnectError) as exc:
            code = getattr(exc, "errno", None) or errno.EIO
            raise SandboxFileSystemError(
                code, f"Sandbox filesystem operation failed: {exc}"
            ) from exc
        finally:
            # Cleanup should not mask an operation error or an interrupted caller.
            with suppress(Exception):
                if client is not None:
                    client.close()
                else:
                    channel.close()

    def get_work_dir(self) -> str:
        """Return the server's initial working directory."""
        with self._session() as client:
            return client.normalize(".")

    def get_user_home_dir(self) -> str:
        """Return the execution user's home directory from the OS account database."""
        with self._session(home=True) as client:
            return client.normalize(".")

    def download_file_stream(
        self, remote_path: str, *, timeout_secs: int | None = None
    ) -> Iterator[bytes]:
        """Yield binary chunks without buffering the file; close on early exit.

        A failure raises even after yielding data. Consumers must discard any
        partial output; ``download_file(path, local_path)`` handles this for you.
        """
        _path(remote_path)
        with self._session(timeout_secs=timeout_secs) as client:
            with client.open(remote_path, "rb") as file:
                while chunk := file.read(_CHUNK_SIZE):
                    yield chunk

    def read_file(self, path: str, *, timeout_secs: int | None = None) -> bytes:
        """Read a file into memory as bytes."""
        return b"".join(self.download_file_stream(path, timeout_secs=timeout_secs))

    def read_text(
        self, path: str, *, encoding: str = "utf-8", timeout_secs: int | None = None
    ) -> str:
        """Read text, rejecting invalid encoding rather than replacing bytes."""
        return self.read_file(path, timeout_secs=timeout_secs).decode(encoding)

    @staticmethod
    def _write(client: SFTPClient, path: str, chunks: Iterable[bytes]) -> None:
        # Refuse symlinks instead of silently replacing the link or writing its
        # target. Preserve permissions of existing regular files on replacement.
        mode = 0o644
        try:
            attrs = client.lstat(path)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(attrs.st_mode or 0):
                raise OSError(errno.ELOOP, "Refusing to replace a symbolic link", path)
            if not stat.S_ISREG(attrs.st_mode or 0):
                raise OSError(errno.EISDIR, "Destination must be a regular file", path)
            mode = stat.S_IMODE(attrs.st_mode or 0)
        temporary = posixpath.join(
            posixpath.dirname(path), f".chalk-upload-{uuid.uuid4().hex}"
        )
        published = False
        try:
            with client.open(temporary, "wx") as destination:
                destination.chmod(0o600)
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("upload chunks must be bytes")
                    for offset in range(0, len(chunk), _CHUNK_SIZE):
                        destination.write(chunk[offset : offset + _CHUNK_SIZE])
                destination.chmod(mode)
            # OpenSSH extension: atomic replacement even when target exists.
            client.posix_rename(temporary, path)
            published = True
        finally:
            if not published:
                with suppress(Exception):
                    client.remove(temporary)

    def upload_file_stream(
        self,
        chunks: Iterable[bytes],
        destination: str,
        *,
        timeout_secs: int | None = None,
    ) -> None:
        """Upload byte chunks atomically; the parent directory must exist.

        Existing permissions are preserved. New files have mode 0644. Symlink
        destinations are rejected. Failed transfers clean up temporary files
        when the connection is usable; never rename incomplete data into place.
        """
        _path(destination)
        with self._session(timeout_secs=timeout_secs) as client:
            self._write(client, destination, chunks)

    def write_file(
        self, path: str, data: str | bytes, *, timeout_secs: int | None = None
    ) -> None:
        """Atomically replace a file with bytes or UTF-8 text."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not isinstance(data, bytes):
            raise TypeError("data must be str or bytes")
        self.upload_file_stream((data,), path, timeout_secs=timeout_secs)

    def upload_file(
        self,
        source: str | Path | bytes,
        destination: str,
        *,
        timeout_secs: int | None = None,
    ) -> None:
        """Stream a local file or upload bytes into an atomic remote replacement."""
        _path(destination)
        with (
            io.BytesIO(source) if isinstance(source, bytes) else open(source, "rb")
        ) as file:
            chunks = iter(lambda: file.read(_CHUNK_SIZE), b"")
            self.upload_file_stream(chunks, destination, timeout_secs=timeout_secs)

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
        """Return bytes, or stream to a local path and atomically replace it.

        A failed download preserves an existing local destination and deletes
        its temporary file. Parent directories must already exist.
        """
        _path(remote_path)
        if local_path is None:
            return self.read_file(remote_path, timeout_secs=timeout_secs)
        target = Path(local_path)
        fd, temporary = tempfile.mkstemp(prefix=".chalk-download-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as file:
                for chunk in self.download_file_stream(
                    remote_path, timeout_secs=timeout_secs
                ):
                    file.write(chunk)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return None

    @staticmethod
    def _info(path: str, attrs: SFTPAttributes) -> SandboxFileInfo:
        mode = attrs.st_mode or 0
        return SandboxFileInfo(
            posixpath.basename(path),
            path,
            attrs.st_size or 0,
            stat.S_ISDIR(mode),
            stat.S_ISLNK(mode),
            format(stat.S_IMODE(mode), "03o"),
            float(attrs.st_mtime or 0),
        )

    def get_file_info(
        self, path: str, *, timeout_secs: int | None = None
    ) -> SandboxFileInfo:
        """Return metadata without following the final symlink."""
        _path(path)
        with self._session(timeout_secs=timeout_secs) as client:
            return self._info(path, client.lstat(path))

    def list_files(
        self, path: str = ".", *, timeout_secs: int | None = None
    ) -> list[SandboxFileInfo]:
        """List immediate children (including hidden files), sorted by name."""
        _path(path)
        with self._session(timeout_secs=timeout_secs) as client:
            return [
                self._info(posixpath.join(path, item.filename), item)
                for item in sorted(
                    client.listdir_attr(path), key=lambda item: item.filename
                )
            ]

    @staticmethod
    def _mkdir(client: SFTPClient, path: str, mode: int) -> None:
        try:
            attrs = client.stat(path)
        except FileNotFoundError:
            parent = posixpath.dirname(path)
            if parent and parent != path:
                SandboxFileSystem._mkdir(client, parent, 0o777)
            try:
                client.mkdir(path, mode)
            except OSError:
                if not stat.S_ISDIR(client.stat(path).st_mode or 0):
                    raise
        else:
            if not stat.S_ISDIR(attrs.st_mode or 0):
                raise NotADirectoryError(errno.ENOTDIR, "Not a directory", path)

    def create_folder(
        self, path: str, mode: str = "755", *, timeout_secs: int | None = None
    ) -> None:
        """Create missing directories; final mode is subject to server umask."""
        _path(path)
        parsed_mode = _mode(mode)
        with self._session(timeout_secs=timeout_secs) as client:
            self._mkdir(client, path, parsed_mode)

    @staticmethod
    def _delete(client: SFTPClient, path: str, recursive: bool) -> None:
        if stat.S_ISDIR(client.lstat(path).st_mode or 0):
            if not recursive:
                raise IsADirectoryError(
                    errno.EISDIR, "Directories require recursive=True", path
                )
            for item in client.listdir_attr(path):
                SandboxFileSystem._delete(
                    client, posixpath.join(path, item.filename), True
                )
            client.rmdir(path)
        else:
            client.remove(path)

    def delete_file(
        self, path: str, *, recursive: bool = False, timeout_secs: int | None = None
    ) -> None:
        """Delete a file/symlink; directories require explicit recursive=True."""
        _path(path)
        with self._session(timeout_secs=timeout_secs) as client:
            self._delete(client, path, recursive)

    def move_files(
        self, source: str, destination: str, *, timeout_secs: int | None = None
    ) -> None:
        """Atomically rename on the same filesystem, replacing the destination."""
        _path(source)
        _path(destination)
        with self._session(timeout_secs=timeout_secs) as client:
            client.posix_rename(source, destination)

    def set_file_permissions(
        self,
        path: str,
        *,
        mode: str | None = None,
        owner: int | None = None,
        group: int | None = None,
        timeout_secs: int | None = None,
    ) -> None:
        """Change mode and/or numeric UID/GID, subject to OS privileges."""
        _path(path)
        parsed_mode = _mode(mode) if mode is not None else None
        for value in (owner, group):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError("owner and group must be nonnegative numeric IDs")
        with self._session(timeout_secs=timeout_secs) as client:
            if parsed_mode is not None:
                client.chmod(path, parsed_mode)
            if owner is not None or group is not None:
                attrs = client.stat(path)
                client.chown(
                    path,
                    owner if owner is not None else attrs.st_uid,
                    group if group is not None else attrs.st_gid,
                )

    @staticmethod
    def _walk(client: SFTPClient, path: str) -> Iterator[str]:
        for item in sorted(client.listdir_attr(path), key=lambda item: item.filename):
            child = posixpath.join(path, item.filename)
            if stat.S_ISDIR(item.st_mode or 0):
                yield from SandboxFileSystem._walk(client, child)
            elif stat.S_ISREG(item.st_mode or 0):
                yield child

    def search_files(
        self, path: str, pattern: str, *, timeout_secs: int | None = None
    ) -> list[str]:
        """Find filenames by glob recursively without following symlinks."""
        _path(path)
        with self._session(timeout_secs=timeout_secs) as client:
            return [
                name
                for name in self._walk(client, path)
                if fnmatch.fnmatchcase(posixpath.basename(name), pattern)
            ]

    def find_files(
        self, path: str, text: str, *, timeout_secs: int | None = None
    ) -> list[FileMatch]:
        """Search UTF-8 content on the client; skip invalid UTF-8 and symlinks."""
        _path(path)
        matches = []
        with self._session(timeout_secs=timeout_secs) as client:
            for name in self._walk(client, path):
                try:
                    with client.open(name, "rb") as file:
                        lines = file.read().decode("utf-8").splitlines()
                except UnicodeDecodeError:
                    continue
                matches.extend(
                    FileMatch(name, n, line)
                    for n, line in enumerate(lines, 1)
                    if text in line
                )
        return matches

    def replace_in_files(
        self, files: list[str], old: str, new: str, *, timeout_secs: int | None = None
    ) -> dict[str, int]:
        """Replace literal UTF-8 text, preserving newlines and file permissions.

        Each file is read into client memory and atomically replaced. The group
        of replacements is not transactional; concurrent edits can be overwritten.
        """
        if not isinstance(old, str) or not old:
            raise ValueError("old must be nonempty text")
        for path in files:
            _path(path)
        result = {}
        with self._session(timeout_secs=timeout_secs) as client:
            for path in files:
                with client.open(path, "rb") as file:
                    text = file.read().decode("utf-8")
                result[path] = text.count(old)
                if result[path]:
                    self._write(client, path, (text.replace(old, new).encode("utf-8"),))
        return result
