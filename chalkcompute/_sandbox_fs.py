"""File operations over sandbox exec, requiring only Python 3 in the image."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chalkcompute._sandbox import Sandbox


class SandboxFileSystemError(RuntimeError):
    """A file operation inside a sandbox failed."""


@dataclass(frozen=True)
class SandboxFileInfo:
    """Metadata for a sandbox path. Permissions are an octal string."""

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


# All caller data travels in JSON argv or raw stdin, never in executable source.
_SCRIPT = r"""
import fnmatch
import json
import os
import pathlib
import shutil
import stat
import sys

operation, params = sys.argv[1], json.loads(sys.argv[2])
p = pathlib.Path(params.get("path", "."))

def info(path):
    st = path.lstat()
    return dict(name=path.name, path=str(path), size=st.st_size,
                is_dir=stat.S_ISDIR(st.st_mode), is_symlink=stat.S_ISLNK(st.st_mode),
                permissions=format(stat.S_IMODE(st.st_mode), "03o"), modified_time=st.st_mtime)

def walk(path):
    if not path.is_dir():
        raise NotADirectoryError(str(path))
    def fail(error):
        raise error
    for root, dirs, files in os.walk(path, followlinks=False, onerror=fail):
        dirs.sort()
        for name in sorted(files):
            child = pathlib.Path(root) / name
            if not child.is_symlink():
                yield child

result = None
if operation == "read":
    sys.stdout.buffer.write(p.read_bytes())
elif operation == "write":
    with p.open("wb") as dst:
        shutil.copyfileobj(sys.stdin.buffer, dst)
elif operation == "mkdir":
    p.mkdir(mode=int(params["mode"], 8), parents=True, exist_ok=True)
elif operation == "delete":
    if p.is_dir() and not p.is_symlink():
        if not params["recursive"]:
            raise IsADirectoryError(str(p))
        shutil.rmtree(p)
    else:
        p.unlink()
elif operation == "info":
    result = info(p)
elif operation == "list":
    result = [info(child) for child in sorted(p.iterdir())]
elif operation == "move":
    p.rename(params["destination"])
elif operation == "permissions":
    if params["mode"] is not None:
        p.chmod(int(params["mode"], 8))
    if params["owner"] is not None or params["group"] is not None:
        shutil.chown(p, user=params["owner"], group=params["group"])
elif operation == "search":
    result = [str(child) for child in walk(p) if fnmatch.fnmatchcase(child.name, params["pattern"])]
elif operation == "find":
    result = []
    for child in walk(p):
        try:
            content = child.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if params["text"] in line:
                result.append(dict(path=str(child), line_number=number, line=line))
elif operation == "replace":
    result = {}
    for name in params["files"]:
        child = pathlib.Path(name)
        with child.open("r", encoding="utf-8", newline="") as src:
            content = src.read()
        count = content.count(params["old"])
        if count:
            with child.open("w", encoding="utf-8", newline="") as dst:
                dst.write(content.replace(params["old"], params["new"]))
        result[name] = count
elif operation == "home":
    result = str(pathlib.Path.home())
elif operation == "cwd":
    result = os.getcwd()
else:
    raise ValueError("Unknown file operation: " + operation)
if result is not None:
    print(json.dumps(result))
"""


def _path(path: str) -> str:
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("path must be a nonempty string without NUL bytes")
    return path


def _mode(mode: str) -> str:
    if (
        not isinstance(mode, str)
        or not 3 <= len(mode) <= 4
        or any(c not in "01234567" for c in mode)
    ):
        raise ValueError("mode must be a three- or four-digit octal string")
    return mode


class SandboxFileSystem:
    """Access as ``sandbox.fs``. Relative paths use the image working directory.

    Requires ``python3`` in custom images. File contents are binary-safe and
    buffered in client memory; use volumes for large datasets. Errors raise
    SandboxFileSystemError. Existing files are overwritten on write/upload.
    """

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def _run(
        self,
        operation: str,
        params: dict[str, object],
        *,
        data: bytes | None = None,
        timeout_secs: int | None = None,
    ) -> bytes:
        args = ("python3", "-c", _SCRIPT, operation, json.dumps(params))
        if data is None:
            result = self._sandbox.exec(*args, timeout_secs=timeout_secs)
            # The hypervisor can reset its output stream before reporting exit.
            # Only downloads are safe to restart; discard all partial output.
            for delay in (0.1, 0.2):
                if not (
                    operation == "read"
                    and result.error
                    and result.error.startswith("hypervisor_error:")
                    and "RST_STREAM" in result.error
                ):
                    break
                time.sleep(delay)
                result = self._sandbox.exec(*args, timeout_secs=timeout_secs)
        else:
            process = self._sandbox.exec_start(*args, timeout_secs=timeout_secs)
            try:
                for offset in range(0, len(data), 64 * 1024):
                    process.write_stdin(data[offset : offset + 64 * 1024])
            finally:
                process.close_stdin()
            # Reattaching cannot replay stdin that was lost on disconnect.
            # Fail the upload instead of reporting a truncated file as success.
            result = process.wait(auto_reattach=False)
        if result.exit_code != 0 or result.error or result.signal:
            message = (
                result.error
                or result.stderr_text
                or f"exit code {result.exit_code}, signal {result.signal}"
            )
            raise SandboxFileSystemError(
                f"Sandbox file operation {operation!r} failed: {message.strip()}"
            )
        return result.stdout

    def read_file(self, path: str, *, timeout_secs: int | None = None) -> bytes:
        """Read a file as bytes."""
        return self._run("read", {"path": _path(path)}, timeout_secs=timeout_secs)

    def write_file(
        self, path: str, data: str | bytes, *, timeout_secs: int | None = None
    ) -> None:
        """Write bytes or UTF-8 text; the parent directory must already exist."""
        _path(path)
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not isinstance(data, bytes):
            raise TypeError("data must be str or bytes")
        self._run("write", {"path": path}, data=data, timeout_secs=timeout_secs)

    def upload_file(
        self,
        source: str | Path | bytes,
        destination: str,
        *,
        timeout_secs: int | None = None,
    ) -> None:
        """Upload a local path or bytes to a remote path."""
        _path(destination)
        data = source if isinstance(source, bytes) else Path(source).read_bytes()
        self.write_file(destination, data, timeout_secs=timeout_secs)

    def download_file(
        self,
        remote_path: str,
        local_path: str | Path | None = None,
        *,
        timeout_secs: int | None = None,
    ) -> bytes:
        """Return file bytes, optionally also saving them to a local path."""
        data = self.read_file(remote_path, timeout_secs=timeout_secs)
        if local_path is not None:
            Path(local_path).write_bytes(data)
        return data

    def create_folder(
        self, path: str, mode: str = "755", *, timeout_secs: int | None = None
    ) -> None:
        """Create directories recursively; existing directories are unchanged.

        ``mode`` applies to the final directory, subject to the process umask.
        """
        self._run(
            "mkdir",
            {"path": _path(path), "mode": _mode(mode)},
            timeout_secs=timeout_secs,
        )

    def delete_file(
        self, path: str, *, recursive: bool = False, timeout_secs: int | None = None
    ) -> None:
        """Delete a file/symlink; directories require explicit recursive=True."""
        self._run(
            "delete",
            {"path": _path(path), "recursive": recursive},
            timeout_secs=timeout_secs,
        )

    def get_file_info(
        self, path: str, *, timeout_secs: int | None = None
    ) -> SandboxFileInfo:
        """Return lstat metadata without following a final symlink."""
        return SandboxFileInfo(
            **json.loads(
                self._run("info", {"path": _path(path)}, timeout_secs=timeout_secs)
            )
        )

    def list_files(
        self, path: str = ".", *, timeout_secs: int | None = None
    ) -> list[SandboxFileInfo]:
        """List immediate children, including hidden files, sorted by name."""
        return [
            SandboxFileInfo(**item)
            for item in json.loads(
                self._run("list", {"path": _path(path)}, timeout_secs=timeout_secs)
            )
        ]

    def move_files(
        self, source: str, destination: str, *, timeout_secs: int | None = None
    ) -> None:
        """Rename a path using OS rename semantics (same filesystem)."""
        self._run(
            "move",
            {"path": _path(source), "destination": _path(destination)},
            timeout_secs=timeout_secs,
        )

    def set_file_permissions(
        self,
        path: str,
        *,
        mode: str | None = None,
        owner: str | None = None,
        group: str | None = None,
        timeout_secs: int | None = None,
    ) -> None:
        """Change permissions/ownership; follows symlinks, subject to OS privileges."""
        self._run(
            "permissions",
            {
                "path": _path(path),
                "mode": _mode(mode) if mode is not None else None,
                "owner": owner,
                "group": group,
            },
            timeout_secs=timeout_secs,
        )

    def search_files(
        self, path: str, pattern: str, *, timeout_secs: int | None = None
    ) -> list[str]:
        """Find filenames by glob recursively, without following symlinks."""
        return json.loads(
            self._run(
                "search",
                {"path": _path(path), "pattern": pattern},
                timeout_secs=timeout_secs,
            )
        )

    def find_files(
        self, path: str, text: str, *, timeout_secs: int | None = None
    ) -> list[FileMatch]:
        """Find literal text in UTF-8 files; skip binary files and symlinks."""
        return [
            FileMatch(**item)
            for item in json.loads(
                self._run(
                    "find",
                    {"path": _path(path), "text": text},
                    timeout_secs=timeout_secs,
                )
            )
        ]

    def replace_in_files(
        self, files: list[str], old: str, new: str, *, timeout_secs: int | None = None
    ) -> dict[str, int]:
        """Replace literal text in UTF-8 files and return replacement counts.

        Each file is updated individually; a failure may leave earlier files
        updated. Parent directories and permissions are unchanged.
        """
        if not isinstance(old, str) or not old:
            raise ValueError("old must be nonempty text")
        return json.loads(
            self._run(
                "replace",
                {"files": [_path(path) for path in files], "old": old, "new": new},
                timeout_secs=timeout_secs,
            )
        )
