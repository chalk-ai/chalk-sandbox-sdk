"""Exercise the exec-backed sandbox filesystem helpers without cloud access."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chalkcompute import (
    ComputeClass,
    ExecResult,
    FileDownload,
    SandboxFileInfo,
    FileMatch,
    FilesystemError,
    FileUpload,
    ReplaceResult,
    Sandbox,
    SandboxError,
    SandboxFileSystemError,
)
from chalkcompute._sandbox_fs import SandboxFileSystem


def _sandbox(*results: ExecResult) -> Sandbox:
    sandbox = Sandbox(sandbox_id="test")
    sandbox.exec = MagicMock(side_effect=list(results))
    return sandbox


def _record(*fields: str) -> bytes:
    return b"".join(field.encode() + b"\0" for field in fields)


def test_list_files_parses_and_sorts_find_output() -> None:
    output = (
        _record(
            "/w/b.txt", "b.txt", "5", "644", "1700000000.5", "f", "root", "root", ""
        )
        + _record(
            "/w/a\nb", "a\nb", "4", "777", "1700000001.0", "l", "app", "staff", "b.txt"
        )
        + _record(
            "/w/dir", "dir", "4096", "2755", "1700000002", "d", "1000", "1000", ""
        )
    )
    sandbox = _sandbox(ExecResult(output, b"", 0))

    infos = sandbox.fs.list_files("/w")

    argv = sandbox.exec.call_args.args
    assert argv[:7] == ("find", "-H", "/w", "-mindepth", "1", "-maxdepth", "1")
    assert [info.name for info in infos] == ["a\nb", "b.txt", "dir"]
    link, file, directory = infos
    assert file == SandboxFileInfo(
        name="b.txt",
        path="/w/b.txt",
        size=5,
        mode=0o644,
        mod_time=datetime(2023, 11, 14, 22, 13, 20, 500000, tzinfo=timezone.utc),
        is_dir=False,
        is_symlink=False,
        owner="root",
        group="root",
    )
    assert link.is_symlink and link.symlink_target == "b.txt" and not link.is_dir
    assert directory.is_dir and directory.mode == 0o2755


def test_get_file_info_does_not_follow_and_guards_option_paths() -> None:
    output = _record("./-x", "-x", "0", "600", "0", "f", "root", "root", "")
    sandbox = _sandbox(ExecResult(output, b"", 0))

    info = sandbox.fs.get_file_info("-x")

    assert sandbox.exec.call_args.args[:4] == ("find", "./-x", "-maxdepth", "0")
    assert info.name == "-x" and info.mode == 0o600


def test_malformed_metadata_is_rejected() -> None:
    sandbox = _sandbox(ExecResult(b"/w\0w\0", b"", 0))
    with pytest.raises(SandboxFileSystemError) as error:
        sandbox.fs.list_files("/w")
    assert error.value.error is FilesystemError.INTERNAL


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (b"find: '/nope': No such file or directory", FilesystemError.NOT_FOUND),
        (
            b"rm: cannot remove '/x': Permission denied",
            FilesystemError.PERMISSION_DENIED,
        ),
        (b"mv: cannot move: Directory not empty", FilesystemError.FAILED_PRECONDITION),
        (b"something odd", FilesystemError.UNSPECIFIED),
    ],
)
def test_command_failures_map_to_filesystem_errors(
    stderr: bytes, expected: FilesystemError
) -> None:
    sandbox = _sandbox(ExecResult(b"", stderr, 1))
    with pytest.raises(SandboxFileSystemError) as error:
        sandbox.fs.delete_file("/x")
    assert error.value.error is expected
    assert stderr.decode() in str(error.value)


def test_mutating_commands_use_argv_and_end_options() -> None:
    ok = ExecResult(b"", b"", 0)
    sandbox = _sandbox(*([ok] * 7))

    sandbox.fs.create_folder("a b/c")
    sandbox.fs.create_folder("d", 0o700, timeout_secs=5)
    sandbox.fs.delete_file("-f")
    sandbox.fs.delete_file("tree", recursive=True)
    sandbox.fs.move_files("old", "new")
    sandbox.fs.set_file_permissions("f", mode=0o4755, owner="app", group="staff")

    calls = [(c.args, c.kwargs["timeout_secs"]) for c in sandbox.exec.call_args_list]
    assert calls == [
        (("mkdir", "-p", "-m", "755", "--", "a b/c"), None),
        (("mkdir", "-p", "-m", "700", "--", "d"), 5),
        (("rm", "-d", "--", "-f"), None),
        (("rm", "-r", "--", "tree"), None),
        (("mv", "--", "old", "new"), None),
        (("chown", "--", "app:staff", "f"), None),
        (("chmod", "--", "4755", "f"), None),
    ]


def test_set_file_permissions_group_only_and_validation() -> None:
    sandbox = _sandbox(ExecResult(b"", b"", 0))
    sandbox.fs.set_file_permissions("f", group="staff")
    assert sandbox.exec.call_args.args == ("chown", "--", ":staff", "f")

    with pytest.raises(ValueError, match="at least one"):
        sandbox.fs.set_file_permissions("f")
    with pytest.raises(ValueError, match="owner"):
        sandbox.fs.set_file_permissions("f", owner="a:b")
    with pytest.raises(ValueError, match="0o7777"):
        sandbox.fs.set_file_permissions("f", mode=0o17777)
    assert sandbox.exec.call_count == 1


def test_search_files_returns_sorted_paths() -> None:
    sandbox = _sandbox(ExecResult(b"/w/z.py\0/w/a\nb.py\0", b"", 0))
    assert sandbox.fs.search_files("/w", "*.py") == ["/w/a\nb.py", "/w/z.py"]
    assert sandbox.exec.call_args.args == (
        "find",
        "-H",
        "/w",
        "-name",
        "*.py",
        "-print0",
    )


def test_find_files_parses_matches_and_treats_no_match_as_empty() -> None:
    output = b"/w/a.py\x0012:x = 'TODO: a:b'\n/w/odd\nname\x003:TODO\n"
    sandbox = _sandbox(ExecResult(output, b"", 0), ExecResult(b"", b"", 1))

    assert sandbox.fs.find_files("/w", "TODO") == [
        FileMatch("/w/a.py", 12, "x = 'TODO: a:b'"),
        FileMatch("/w/odd\nname", 3, "TODO"),
    ]
    assert sandbox.exec.call_args.args[-5:] == ("-F", "-e", "TODO", "--", "/w")
    assert sandbox.fs.find_files("/w", "-v", regex=True) == []
    assert sandbox.exec.call_args.args[-5:] == ("-E", "-e", "-v", "--", "/w")


def test_find_files_raises_on_grep_error() -> None:
    sandbox = _sandbox(ExecResult(b"", b"grep: /nope: No such file or directory", 2))
    with pytest.raises(SandboxFileSystemError) as error:
        sandbox.fs.find_files("/nope", "x")
    assert error.value.error is FilesystemError.NOT_FOUND


class _FakeTransfer:
    """Stand-in for the upload/download RPCs, backed by a dict."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.modes: dict[str, int | None] = {}

    def upload(
        self,
        fs: SandboxFileSystem,
        chunks: Iterable[bytes],
        path: str,
        *,
        mode: int | None,
        timeout_secs: int | None,
    ) -> None:
        self.files[path] = b"".join(chunks)
        self.modes[path] = mode

    def download(
        self, fs: SandboxFileSystem, path: str, *, timeout_secs: int | None
    ) -> Iterator[bytes]:
        if path not in self.files:
            raise SandboxFileSystemError("missing", error=FilesystemError.NOT_FOUND)
        yield self.files[path]

    def __enter__(self) -> _FakeTransfer:
        self._patches = [
            patch.object(
                SandboxFileSystem, "_upload", autospec=True, side_effect=self.upload
            ),
            patch.object(
                SandboxFileSystem, "_download", autospec=True, side_effect=self.download
            ),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *_args: object) -> None:
        for p in self._patches:
            p.stop()


def test_upload_and_download_variants(tmp_path: Path) -> None:
    local = tmp_path / "local.txt"
    local.write_bytes(b"from disk")
    sandbox = Sandbox(sandbox_id="test")
    with _FakeTransfer({}) as transfer:
        sandbox.fs.upload_file(b"raw", "/raw")
        sandbox.fs.upload_file(local, "/disk", mode=0o600)
        sandbox.fs.upload_files(
            [FileUpload(b"one", "/one"), FileUpload(str(local), "/two", 0o755)]
        )
        assert transfer.files == {
            "/raw": b"raw",
            "/disk": b"from disk",
            "/one": b"one",
            "/two": b"from disk",
        }
        assert transfer.modes["/disk"] == 0o600 and transfer.modes["/two"] == 0o755

        assert sandbox.fs.download_file("/raw") == b"raw"
        out = tmp_path / "out.txt"
        assert sandbox.fs.download_files(
            [FileDownload("/one"), FileDownload("/disk", out)]
        ) == [b"one", None]
        assert out.read_bytes() == b"from disk"

        with pytest.raises(TypeError):
            sandbox.fs.upload_files([("/x", b"x")])  # type: ignore[list-item]


def test_replace_in_files_preserves_mode_and_reports_per_file() -> None:
    info = _record("/a", "a", "0", "750", "0", "f", "root", "root", "")
    sandbox = _sandbox(ExecResult(info, b"", 0))
    files = {"/a": b"x = 1\nx = 1\n", "/b": b"unchanged\n"}
    with _FakeTransfer(files) as transfer:
        results = sandbox.fs.replace_in_files(
            ["/a", "/b", "/missing"], "x = 1", "x = 2"
        )

    assert results == [
        ReplaceResult("/a", True, 2),
        ReplaceResult("/b", True, 0),
        ReplaceResult("/missing", False, error="missing"),
    ]
    assert files["/a"] == b"x = 2\nx = 2\n"
    assert transfer.modes == {"/a": 0o750}
    with pytest.raises(ValueError):
        sandbox.fs.replace_in_files(["/a"], "", "y")


def test_add_file_queues_until_run(tmp_path: Path) -> None:
    local = tmp_path / "local.txt"
    local.write_bytes(b"v1")
    sandbox = Sandbox(image="custom")
    sandbox.fs.add_file(b"script", "/run.sh", mode=0o755)
    sandbox.fs.add_file(local, "/local.txt")
    # Local files are read at upload time, not when they are added.
    local.write_bytes(b"v2")

    with pytest.raises(FileNotFoundError):
        sandbox.fs.add_file(tmp_path / "missing", "/missing")
    with pytest.raises(SandboxError, match="wait=True"):
        sandbox.run(wait=False)

    client = MagicMock()

    def instantiate(sb: Sandbox, *_args: object, **_kwargs: object) -> None:
        sb._id = "started"

    client._instantiate.side_effect = instantiate
    sandbox._client = client
    with _FakeTransfer({}) as transfer:
        sandbox.run()
        assert transfer.files == {"/run.sh": b"script", "/local.txt": b"v2"}
        assert transfer.modes["/run.sh"] == 0o755
        assert sandbox._pending_files == []

        # Once running, add_file uploads immediately.
        sandbox.fs.add_file(b"later", "/later")
        assert transfer.files["/later"] == b"later"


def _gnu_tools_available() -> bool:
    if shutil.which("find") is None or shutil.which("grep") is None:
        return False
    probe = subprocess.run(
        ["find", "/", "-maxdepth", "0", "-printf", "%y"], capture_output=True
    )
    return probe.returncode == 0 and probe.stdout == b"d"


@pytest.mark.skipif(not _gnu_tools_available(), reason="requires GNU findutils")
def test_commands_against_real_gnu_tools(tmp_path: Path) -> None:
    """Run the generated argv with the local GNU tools a Debian image provides."""

    def run(command: str, *args: str, timeout_secs: int | None = None) -> ExecResult:
        process = subprocess.run([command, *args], capture_output=True, cwd=tmp_path)
        return ExecResult(process.stdout, process.stderr, process.returncode)

    sandbox = Sandbox(sandbox_id="local")
    sandbox.exec = run  # type: ignore[method-assign]
    root = str(tmp_path / "root")

    sandbox.fs.create_folder(root + "/pkg", 0o700)
    (tmp_path / "root/pkg/mod.py").write_text("# TODO: one\nvalue = 1\n")
    (tmp_path / "root/notes.txt").write_text("nothing\n")
    (tmp_path / "root/link").symlink_to("notes.txt")

    listing = sandbox.fs.list_files(root)
    assert [info.name for info in listing] == ["link", "notes.txt", "pkg"]
    assert listing[0].is_symlink and listing[0].symlink_target == "notes.txt"
    assert listing[2].is_dir and listing[2].mode == 0o700
    assert sandbox.fs.get_file_info(root + "/notes.txt").size == 8

    assert sandbox.fs.search_files(root, "*.py") == [root + "/pkg/mod.py"]
    assert sandbox.fs.find_files(root, "TODO") == [
        FileMatch(root + "/pkg/mod.py", 1, "# TODO: one")
    ]
    assert sandbox.fs.find_files(root, "^val.*= [0-9]$", regex=True)[0].line == 2
    assert sandbox.fs.find_files(root, "absent") == []

    sandbox.fs.set_file_permissions(root + "/notes.txt", mode=0o640)
    assert sandbox.fs.get_file_info(root + "/notes.txt").mode == 0o640
    sandbox.fs.move_files(root + "/notes.txt", root + "/renamed.txt")
    sandbox.fs.delete_file(root + "/link")
    with pytest.raises(SandboxFileSystemError) as not_empty:
        sandbox.fs.delete_file(root + "/pkg")
    assert not_empty.value.error is FilesystemError.FAILED_PRECONDITION
    sandbox.fs.delete_file(root + "/pkg", recursive=True)
    assert [info.name for info in sandbox.fs.list_files(root)] == ["renamed.txt"]
    with pytest.raises(SandboxFileSystemError) as missing:
        sandbox.fs.get_file_info(root + "/pkg")
    assert missing.value.error is FilesystemError.NOT_FOUND


def test_upload_creates_missing_parent_and_retries() -> None:
    sandbox = _sandbox(ExecResult(b"", b"", 0))
    attempts: list[str] = []

    def upload(
        fs: SandboxFileSystem,
        chunks: Iterable[bytes],
        path: str,
        *,
        mode: int | None,
        timeout_secs: int | None,
    ) -> None:
        attempts.append(b"".join(chunks).decode())
        if len(attempts) == 1:
            raise SandboxFileSystemError("missing", error=FilesystemError.NOT_FOUND)

    with patch.object(SandboxFileSystem, "_upload", autospec=True, side_effect=upload):
        sandbox.fs.upload_file(b"data", "/a/b/file.txt")

    assert attempts == ["data", "data"]
    assert sandbox.exec.call_args.args == ("mkdir", "-p", "--", "/a/b")

    with patch.object(
        SandboxFileSystem,
        "_upload",
        autospec=True,
        side_effect=SandboxFileSystemError("x", error=FilesystemError.NOT_FOUND),
    ):
        with pytest.raises(SandboxFileSystemError):
            sandbox.fs.upload_file(b"data", "relative.txt")
    assert sandbox.exec.call_count == 1


def test_write_bytes_creates_missing_parent() -> None:
    sandbox = _sandbox(ExecResult(b"", b"", 0))
    attempts = 0

    def upload(
        fs: SandboxFileSystem,
        chunks: Iterable[bytes],
        path: str,
        *,
        mode: int | None,
        timeout_secs: int | None,
    ) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SandboxFileSystemError("missing", error=FilesystemError.NOT_FOUND)

    with patch.object(SandboxFileSystem, "_upload", autospec=True, side_effect=upload):
        sandbox.fs.write_bytes("/new/dir/file", b"data")
    assert attempts == 2
    assert sandbox.exec.call_args.args == ("mkdir", "-p", "--", "/new/dir")


def test_run_terminates_sandbox_when_queued_upload_fails() -> None:
    sandbox = Sandbox(image="custom")
    sandbox.fs.add_file(b"data", "/data")
    client = MagicMock()

    def instantiate(sb: Sandbox, *_args: object, **_kwargs: object) -> None:
        sb._id = "started"

    client._instantiate.side_effect = instantiate
    sandbox._client = client
    failure = SandboxFileSystemError(
        "disk full", error=FilesystemError.RESOURCE_EXHAUSTED
    )
    with patch.object(SandboxFileSystem, "_upload", autospec=True, side_effect=failure):
        with pytest.raises(SandboxFileSystemError, match="disk full"):
            sandbox.run()

    client._stub.terminate_sandbox.assert_called_once()
    assert client._stub.terminate_sandbox.call_args.args[0].id == "started"
    # The handle is reset and keeps its queue, so run() can be retried.
    assert sandbox.id is None
    assert [file.destination for file in sandbox._pending_files] == ["/data"]


def test_run_reports_terminate_failure_as_note() -> None:
    sandbox = Sandbox(image="custom")
    sandbox.fs.add_file(b"data", "/data")
    client = MagicMock()

    def instantiate(sb: Sandbox, *_args: object, **_kwargs: object) -> None:
        sb._id = "started"

    client._instantiate.side_effect = instantiate
    client._stub.terminate_sandbox.side_effect = RuntimeError("terminate broke")
    sandbox._client = client
    with patch.object(
        SandboxFileSystem,
        "_upload",
        autospec=True,
        side_effect=SandboxFileSystemError("x", error=FilesystemError.INTERNAL),
    ):
        with pytest.raises(SandboxFileSystemError) as error:
            sandbox.run()
    assert error.value.__notes__ == [
        "Terminating sandbox started also failed: terminate broke"
    ]


def test_k8s_sandboxes_reject_session_operations_locally() -> None:
    # Real exec(): the guard must fire before any client or RPC is needed.
    sandbox = Sandbox(sandbox_id="test")
    sandbox._compute_class = ComputeClass.K8S

    for operation in (
        lambda: sandbox.exec("true"),
        lambda: sandbox.fs.list_files("/"),
        lambda: sandbox.fs.write_bytes("/x", b"x"),
        lambda: sandbox.fs.read_bytes("/x"),
        lambda: sandbox.git.init("repo"),
    ):
        with pytest.raises(SandboxError, match="HOST compute class"):
            operation()
    assert sandbox._client is None

    unstarted = Sandbox(image="custom", compute_class=ComputeClass.K8S)
    with pytest.raises(SandboxError, match="HOST compute class"):
        unstarted.fs.add_file(b"x", "/x")
    assert unstarted._pending_files == []

    # HOST and unknown (attached by ID) classes are left to the server.
    for compute_class in (ComputeClass.HOST, None):
        allowed = _sandbox(ExecResult(b"", b"", 0))
        allowed._compute_class = compute_class
        allowed.fs.create_folder("/ok")
