"""File transfer operations for running sandboxes."""

from __future__ import annotations

import os
import posixpath
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, overload

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


@dataclass(frozen=True)
class SandboxFileInfo:
    """Metadata for one sandbox path, as reported by ``lstat``.

    Attributes
    ----------
    name
        Final path component.
    path
        Path inside the sandbox, as passed or joined with the listed directory.
    size
        Size in bytes. For a symlink, the length of its target.
    mode
        Permission bits, including setuid, setgid, and sticky bits.
    mod_time
        Last modification time, in UTC.
    is_dir
        Whether the path is a directory. False for a symlink to a directory.
    is_symlink
        Whether the path is a symbolic link.
    owner
        Owning user name, or numeric ID if the name is unknown.
    group
        Owning group name, or numeric ID if the name is unknown.
    symlink_target
        Target of a symbolic link; otherwise None.
    """

    name: str
    path: str
    size: int
    mode: int
    mod_time: datetime
    is_dir: bool
    is_symlink: bool
    owner: str
    group: str
    symlink_target: str | None = None


@dataclass(frozen=True)
class FileUpload:
    """One file for :meth:`SandboxFileSystem.upload_files`.

    Attributes
    ----------
    source
        File contents as bytes, or a local file path.
    destination
        Path inside the sandbox.
    mode
        Optional permission bits for the sandbox file.
    """

    source: bytes | str | Path
    destination: str
    mode: int | None = None


@dataclass(frozen=True)
class FileDownload:
    """One file for :meth:`SandboxFileSystem.download_files`.

    Attributes
    ----------
    source
        Path inside the sandbox.
    destination
        Local path to write. None returns the contents as bytes instead.
    """

    source: str
    destination: str | Path | None = None


@dataclass(frozen=True)
class FileMatch:
    """A line that matched :meth:`SandboxFileSystem.find_files`.

    Attributes
    ----------
    file
        Path of the matching file inside the sandbox.
    line
        One-based line number.
    content
        Text of the matching line, without its trailing newline.
    """

    file: str
    line: int
    content: str


@dataclass(frozen=True)
class ReplaceResult:
    """Outcome of :meth:`SandboxFileSystem.replace_in_files` for one file.

    Attributes
    ----------
    file
        Path inside the sandbox.
    success
        Whether the file was read and, if it contained the pattern, rewritten.
    replacements
        Number of occurrences replaced.
    error
        Error message when success is False; otherwise None.
    """

    file: str
    success: bool
    replacements: int = 0
    error: str | None = None


def _path(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("path must be a nonempty string without NUL bytes")
    return value


def _operand(value: str) -> str:
    """Keep a path from being parsed as an option by coreutils/findutils."""
    value = _path(value)
    return "./" + value if value.startswith("-") else value


# One record per path: nine NUL-terminated fields, parsed by _parse_file_info.
_FIND_FORMAT = "%p\\0%f\\0%s\\0%m\\0%T@\\0%y\\0%u\\0%g\\0%l\\0"
_FIND_FIELDS = 9

# Exit-status-independent stderr fragments from GNU coreutils/findutils/grep.
_STDERR_ERRORS = (
    ("No such file or directory", FilesystemError.NOT_FOUND),
    ("Permission denied", FilesystemError.PERMISSION_DENIED),
    ("Operation not permitted", FilesystemError.PERMISSION_DENIED),
    ("File exists", FilesystemError.ALREADY_EXISTS),
    ("Not a directory", FilesystemError.FAILED_PRECONDITION),
    ("Is a directory", FilesystemError.FAILED_PRECONDITION),
    ("Directory not empty", FilesystemError.FAILED_PRECONDITION),
    ("No space left on device", FilesystemError.RESOURCE_EXHAUSTED),
)


def _decode(value: bytes) -> str:
    return value.decode("utf-8", "surrogateescape")


def _parse_file_info(output: bytes) -> list[SandboxFileInfo]:
    fields = output.split(b"\0")
    # The format terminates every field, so a trailing empty element remains.
    if fields[-1] != b"" or (len(fields) - 1) % _FIND_FIELDS:
        raise SandboxFileSystemError(
            "Sandbox returned malformed file metadata",
            error=FilesystemError.INTERNAL,
        )
    infos = []
    for start in range(0, len(fields) - 1, _FIND_FIELDS):
        path, name, size, mode, mtime, kind, owner, group, target = (
            _decode(value) for value in fields[start : start + _FIND_FIELDS]
        )
        infos.append(
            SandboxFileInfo(
                name=name,
                path=path,
                size=int(size),
                mode=int(mode, 8),
                mod_time=datetime.fromtimestamp(float(mtime), tz=timezone.utc),
                is_dir=kind == "d",
                is_symlink=kind == "l",
                owner=owner,
                group=group,
                symlink_target=target if kind == "l" else None,
            )
        )
    return infos


def _parse_matches(output: bytes) -> list[FileMatch]:
    # grep -HnZ emits "file\0line:content\n"; file names may contain newlines.
    matches = []
    position = 0
    while position < len(output):
        separator = output.index(b"\0", position)
        end = output.find(b"\n", separator)
        if end == -1:
            end = len(output)
        line, _, content = output[separator + 1 : end].partition(b":")
        matches.append(
            FileMatch(_decode(output[position:separator]), int(line), _decode(content))
        )
        position = end + 1
    return matches


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
            errno = value.posix_errno if value.has_field("posix_errno") else None
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

    def _exec(
        self,
        *argv: str,
        timeout_secs: int | None,
        ok_exit_codes: tuple[int, ...] = (0,),
    ) -> bytes:
        _timeout_ms(timeout_secs)
        result = self._sandbox.exec(*argv, timeout_secs=timeout_secs)
        if result.exit_code in ok_exit_codes and not result.error and not result.signal:
            return result.stdout
        message = (
            result.error
            or result.stderr_text
            or result.stdout_text
            or f"exit code {result.exit_code}, signal {result.signal}"
        ).strip()
        error = next(
            (error for fragment, error in _STDERR_ERRORS if fragment in message),
            FilesystemError.UNSPECIFIED,
        )
        raise SandboxFileSystemError(f"{argv[0]} failed: {message}", error=error)

    def _upload_source(
        self,
        source: bytes | str | Path,
        path: str,
        *,
        mode: int | None,
        timeout_secs: int | None,
    ) -> None:
        if not isinstance(source, (bytes, str, Path)):
            raise TypeError("source must be bytes or a local path")

        def upload() -> None:
            if isinstance(source, bytes):
                self._upload((source,), path, mode=mode, timeout_secs=timeout_secs)
                return
            with Path(source).open("rb") as file:
                self._upload(
                    iter(lambda: file.read(_CHUNK_SIZE), b""),
                    path,
                    mode=mode,
                    timeout_secs=timeout_secs,
                )

        try:
            upload()
        except SandboxFileSystemError as error:
            parent = posixpath.dirname(_path(path))
            if error.error is not FilesystemError.NOT_FOUND or not parent:
                raise
            # The upload RPC does not create parents; create them and retry once.
            self._exec("mkdir", "-p", "--", parent, timeout_secs=timeout_secs)
            upload()

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
        sandbox_id = self._sandbox._require_session()
        client = self._sandbox._require_client()
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
        request = fs_pb.DownloadFileRequest(
            sandbox_id=self._sandbox._require_session(),
            path=source,
        )
        client = self._sandbox._require_client()
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
        """Write ``data`` to ``path`` in the sandbox, creating missing parents."""
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        self._upload_source(data, path, mode=mode, timeout_secs=timeout_secs)

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
        local_path: bytes | str | Path,
        path: str,
        *,
        mode: int | None = None,
        timeout_secs: int | None = None,
    ) -> None:
        """Upload a local file, or ``bytes`` contents, to ``path`` in the sandbox.

        Missing parent directories are created.
        """
        self._upload_source(local_path, path, mode=mode, timeout_secs=timeout_secs)

    @_instrumented("chalkcompute.sandbox.fs.upload_files")
    def upload_files(
        self,
        files: Iterable[FileUpload],
        *,
        timeout_secs: int | None = None,
    ) -> None:
        """Upload several files in order, stopping at the first failure.

        Missing parent directories are created.

        Examples
        --------
        >>> sandbox.fs.upload_files([
        ...     FileUpload(b"print('hi')\\n", "app/main.py"),
        ...     FileUpload("config.toml", "app/config.toml", mode=0o600),
        ... ])
        """
        for file in files:
            if not isinstance(file, FileUpload):
                raise TypeError("files must contain FileUpload values")
            self._upload_source(
                file.source,
                file.destination,
                mode=file.mode,
                timeout_secs=timeout_secs,
            )

    def add_file(
        self,
        source: bytes | str | Path,
        path: str,
        *,
        mode: int | None = None,
    ) -> None:
        """Place a file in the sandbox, before or after :meth:`Sandbox.run`.

        On a running sandbox this uploads immediately, like :meth:`upload_file`.
        On a sandbox that has not started, the file is queued and uploaded by
        ``run()`` once the sandbox is ready, so files can be declared alongside
        the rest of the sandbox configuration. Queued files arrive after the
        entrypoint starts; ``run(wait=False)`` rejects a sandbox with queued
        files because it returns before uploads are possible.

        Parameters
        ----------
        source
            File contents as bytes, or a local file path. A local file is read
            when it is uploaded, not when it is added.
        path
            Destination path inside the sandbox. Parent directories are created.
        mode
            Optional permission bits for the sandbox file, such as ``0o755``.

        Examples
        --------
        >>> sandbox = Sandbox(image=Image.sandbox())
        >>> sandbox.fs.add_file(b"#!/bin/sh\\necho ready\\n", "/workspace/run.sh", mode=0o755)
        >>> sandbox.fs.add_file("requirements.txt", "/workspace/requirements.txt")
        >>> sandbox.run()
        """
        file = FileUpload(source, _path(path), _mode(mode))
        if isinstance(source, (str, Path)):
            if not Path(source).is_file():
                raise FileNotFoundError(f"Local file not found: {source}")
        elif not isinstance(source, bytes):
            raise TypeError("source must be bytes or a local path")
        self._sandbox._check_session_support()
        if self._sandbox._id is None:
            self._sandbox._pending_files.append(file)
        else:
            self.upload_files([file])

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

    @overload
    def download_file(
        self,
        path: str,
        local_path: None = None,
        *,
        timeout_secs: int | None = None,
    ) -> bytes: ...

    @overload
    def download_file(
        self,
        path: str,
        local_path: str | Path,
        *,
        timeout_secs: int | None = None,
    ) -> None: ...

    @_instrumented("chalkcompute.sandbox.fs.download_file")
    def download_file(
        self,
        path: str,
        local_path: str | Path | None = None,
        *,
        timeout_secs: int | None = None,
    ) -> bytes | None:
        """Download a sandbox file, atomically replacing ``local_path``.

        With no ``local_path``, return the contents as bytes instead.
        """
        if local_path is None:
            return self.read_bytes(path, timeout_secs=timeout_secs)
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
        return None

    @_instrumented("chalkcompute.sandbox.fs.download_files")
    def download_files(
        self,
        files: Iterable[FileDownload],
        *,
        timeout_secs: int | None = None,
    ) -> list[bytes | None]:
        """Download several files in order, stopping at the first failure.

        Returns
        -------
        list[bytes | None]
            One entry per request: the contents when ``destination`` is None,
            otherwise None after writing the local file.
        """
        results: list[bytes | None] = []
        for file in files:
            if not isinstance(file, FileDownload):
                raise TypeError("files must contain FileDownload values")
            results.append(
                self.download_file(
                    file.source, file.destination, timeout_secs=timeout_secs
                )
            )
        return results

    @_instrumented("chalkcompute.sandbox.fs.list_files")
    def list_files(
        self,
        path: str = ".",
        *,
        timeout_secs: int | None = None,
    ) -> list[SandboxFileInfo]:
        """List a directory's entries, sorted by name, without recursing.

        A symlink passed as ``path`` is followed; symlinks inside it are not.

        Examples
        --------
        >>> for info in sandbox.fs.list_files("/workspace"):
        ...     print(info.name, info.size, oct(info.mode), info.is_dir)
        """
        output = self._exec(
            "find",
            "-H",
            _operand(path),
            "-mindepth",
            "1",
            "-maxdepth",
            "1",
            "-printf",
            _FIND_FORMAT,
            timeout_secs=timeout_secs,
        )
        return sorted(_parse_file_info(output), key=lambda info: info.name)

    @_instrumented("chalkcompute.sandbox.fs.get_file_info")
    def get_file_info(
        self,
        path: str,
        *,
        timeout_secs: int | None = None,
    ) -> SandboxFileInfo:
        """Return metadata for one path without following a final symlink.

        Examples
        --------
        >>> sandbox.fs.get_file_info("/workspace/run.sh").mode == 0o755
        True
        """
        output = self._exec(
            "find",
            _operand(path),
            "-maxdepth",
            "0",
            "-printf",
            _FIND_FORMAT,
            timeout_secs=timeout_secs,
        )
        (info,) = _parse_file_info(output)
        return info

    @_instrumented("chalkcompute.sandbox.fs.create_folder")
    def create_folder(
        self,
        path: str,
        mode: int = 0o755,
        *,
        timeout_secs: int | None = None,
    ) -> None:
        """Create a directory and any missing parents; existing ones are kept.

        ``mode`` applies to the final directory only, and only if it is created.
        """
        self._exec(
            "mkdir",
            "-p",
            "-m",
            format(_mode(mode), "o"),
            "--",
            _path(path),
            timeout_secs=timeout_secs,
        )

    @_instrumented("chalkcompute.sandbox.fs.delete_file")
    def delete_file(
        self,
        path: str,
        *,
        recursive: bool = False,
        timeout_secs: int | None = None,
    ) -> None:
        """Delete a file, symlink, or empty directory.

        Pass ``recursive=True`` to delete a directory and its contents. A
        missing path raises :class:`SandboxFileSystemError` either way.
        """
        self._exec(
            "rm",
            "-r" if recursive else "-d",
            "--",
            _path(path),
            timeout_secs=timeout_secs,
        )

    @_instrumented("chalkcompute.sandbox.fs.move_files")
    def move_files(
        self,
        source: str,
        destination: str,
        *,
        timeout_secs: int | None = None,
    ) -> None:
        """Move or rename ``source``, with ``mv`` semantics.

        An existing directory at ``destination`` receives ``source`` inside it;
        an existing file at ``destination`` is replaced.
        """
        self._exec(
            "mv",
            "--",
            _path(source),
            _path(destination),
            timeout_secs=timeout_secs,
        )

    @_instrumented("chalkcompute.sandbox.fs.set_file_permissions")
    def set_file_permissions(
        self,
        path: str,
        *,
        mode: int | None = None,
        owner: str | None = None,
        group: str | None = None,
        timeout_secs: int | None = None,
    ) -> None:
        """Change a path's permission bits and/or ownership.

        Changing ownership generally requires the sandbox user to be root.

        Examples
        --------
        >>> sandbox.fs.set_file_permissions("/workspace/run.sh", mode=0o755)
        >>> sandbox.fs.set_file_permissions("/srv/data", owner="app", group="app")
        """
        target = _path(path)
        if mode is None and owner is None and group is None:
            raise ValueError("Specify at least one of mode, owner, or group")
        for name, value in (("owner", owner), ("group", group)):
            if value is not None and (
                not isinstance(value, str) or not value or ":" in value
            ):
                raise ValueError(f"{name} must be a nonempty string without ':'")
        if owner is not None or group is not None:
            ownership = (owner or "") + (f":{group}" if group is not None else "")
            self._exec("chown", "--", ownership, target, timeout_secs=timeout_secs)
        if mode is not None:
            self._exec(
                "chmod",
                "--",
                format(_mode(mode), "o"),
                target,
                timeout_secs=timeout_secs,
            )

    @_instrumented("chalkcompute.sandbox.fs.search_files")
    def search_files(
        self,
        path: str,
        pattern: str,
        *,
        timeout_secs: int | None = None,
    ) -> list[str]:
        """Recursively find paths whose name matches a shell glob, such as ``*.py``.

        Examples
        --------
        >>> sandbox.fs.search_files("/workspace", "*.py")
        ['/workspace/main.py', '/workspace/pkg/util.py']
        """
        output = self._exec(
            "find",
            "-H",
            _operand(path),
            "-name",
            _path(pattern),
            "-print0",
            timeout_secs=timeout_secs,
        )
        return sorted(_decode(entry) for entry in output.split(b"\0") if entry)

    @_instrumented("chalkcompute.sandbox.fs.find_files")
    def find_files(
        self,
        path: str,
        pattern: str,
        *,
        regex: bool = False,
        timeout_secs: int | None = None,
    ) -> list[FileMatch]:
        """Recursively search text file contents for ``pattern``.

        ``pattern`` is a literal string by default; pass ``regex=True`` to use a
        POSIX extended regular expression. Binary files are skipped.

        Examples
        --------
        >>> for match in sandbox.fs.find_files("/workspace", "TODO"):
        ...     print(f"{match.file}:{match.line}: {match.content}")
        """
        output = self._exec(
            "grep",
            "-r",
            "-H",
            "-n",
            "-I",
            "-Z",
            "-E" if regex else "-F",
            "-e",
            _path(pattern),
            "--",
            _path(path),
            timeout_secs=timeout_secs,
            # grep exits 1 when nothing matches.
            ok_exit_codes=(0, 1),
        )
        return _parse_matches(output)

    @_instrumented("chalkcompute.sandbox.fs.replace_in_files")
    def replace_in_files(
        self,
        files: list[str],
        pattern: str,
        new_value: str,
        *,
        timeout_secs: int | None = None,
    ) -> list[ReplaceResult]:
        """Replace every literal occurrence of ``pattern`` in each file.

        Each file is downloaded, edited locally, and uploaded with its original
        permission bits only if it changed. A failure for one file is reported
        in its result rather than raised, and does not stop the others.

        Examples
        --------
        >>> results = sandbox.fs.replace_in_files(["app/config.py"], "DEBUG = True", "DEBUG = False")
        >>> [result.replacements for result in results]
        [1]
        """
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("pattern must be a nonempty string")
        if not isinstance(new_value, str):
            raise TypeError("new_value must be a string")
        old, new = pattern.encode(), new_value.encode()
        results = []
        for file in files:
            try:
                data = self.read_bytes(_path(file), timeout_secs=timeout_secs)
                count = data.count(old)
                if count:
                    mode = self.get_file_info(file, timeout_secs=timeout_secs).mode
                    self.write_bytes(
                        file,
                        data.replace(old, new),
                        mode=mode,
                        timeout_secs=timeout_secs,
                    )
                results.append(ReplaceResult(file, True, count))
            except SandboxFileSystemError as error:
                results.append(ReplaceResult(file, False, error=str(error)))
        return results
