"""Protocol tests and optional integration tests against the native entrypoint."""

from __future__ import annotations

import errno
import json
import os
import struct
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chalkcompute import Sandbox, SandboxError, SandboxFileSystemError
from chalkcompute._sandbox_fs import _Session, _MAX_FRAME, _ENTRYPOINT


@pytest.fixture
def sandbox(tmp_path):
    binary = os.environ.get("CHALK_SANDBOX_FS_TEST_BINARY")
    if not binary:
        pytest.skip(
            "set CHALK_SANDBOX_FS_TEST_BINARY to the compiled native entrypoint"
        )
    processes = []

    class Process:
        def __init__(self):
            self.child = subprocess.Popen(
                [binary, "fs", "--protocol=1"],
                cwd=tmp_path,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            processes.append(self.child)

        def write_stdin(self, data):
            self.child.stdin.write(data)
            self.child.stdin.flush()

        def close_stdin(self):
            if not self.child.stdin.closed:
                self.child.stdin.close()

        def stdout_bytes(self):
            while chunk := self.child.stdout.read1(32768):
                yield chunk
            stderr = self.child.stderr.read()
            assert self.child.wait(timeout=10) == 0, stderr

    handle = Sandbox(sandbox_id="local-native-fs")

    def start(command, *args, timeout_secs=None):
        assert command == _ENTRYPOINT
        assert args == ("fs", "--protocol=1")
        return Process()

    handle.exec_start = start
    try:
        yield handle
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()


def test_files_binary_paths_and_metadata(sandbox, tmp_path):
    sandbox.fs.create_folder("dir", "700")
    path = "dir/ - ' ; $(touch bad)\nfile.bin"
    data = bytes(range(256)) * 1024
    sandbox.fs.write_file(path, data)
    assert sandbox.fs.read_file(path) == data
    assert sandbox.fs.get_file_info(path).size == len(data)
    assert sandbox.fs.list_files("dir")[0].name == Path(path).name
    assert not (tmp_path / "bad").exists()
    sandbox.fs.set_file_permissions(path, mode="600")
    assert sandbox.fs.get_file_info(path).permissions == "600"
    local = tmp_path / "download"
    assert sandbox.fs.download_file(path, local) is None
    assert local.read_bytes() == data
    sandbox.fs.upload_file(local, "dir/upload")
    assert sandbox.fs.read_file("dir/upload") == data
    sandbox.fs.upload_file(b"", "dir/empty")
    assert sandbox.fs.read_file("dir/empty") == b""
    sandbox.fs.move_files("dir/empty", "dir/moved")
    with pytest.raises(SandboxFileSystemError):
        sandbox.fs.delete_file("dir")
    sandbox.fs.delete_file("dir", recursive=True)
    assert not (tmp_path / "dir").exists()
    assert sandbox.get_work_dir() == str(tmp_path)
    accounts = [
        line.split(":") for line in Path("/etc/passwd").read_text().splitlines()
    ]
    account = next(
        (
            fields
            for fields in accounts
            if len(fields) >= 7 and fields[2] == str(os.geteuid())
        ),
        None,
    )
    if account is not None:
        assert sandbox.get_user_home_dir() == account[5]
    else:
        with pytest.raises(SandboxFileSystemError) as error:
            sandbox.get_user_home_dir()
        assert error.value.errno == errno.ENOENT


def test_find_replace_symlinks_errors(sandbox, tmp_path):
    sandbox.fs.create_folder("dir/nested")
    sandbox.fs.write_file("dir/nested/a.txt", "hello\nhello world\n")
    sandbox.fs.write_file("dir/binary", b"\xff\x00")
    (tmp_path / "outside.txt").write_text("hello")
    (tmp_path / "dir/link").symlink_to(tmp_path / "outside.txt")
    assert sandbox.fs.get_file_info("dir/link").is_symlink
    assert sandbox.fs.search_files("dir", "*.txt") == ["dir/nested/a.txt"]
    matches = sandbox.fs.find_files("dir", "hello")
    assert [m.line_number for m in matches] == [1, 2]
    assert sandbox.fs.replace_in_files(["dir/nested/a.txt"], "hello", "bye") == {
        "dir/nested/a.txt": 2
    }
    assert sandbox.fs.read_file("dir/nested/a.txt") == b"bye\nbye world\n"
    sandbox.fs.delete_file("dir/link", recursive=True)
    assert (tmp_path / "outside.txt").exists()
    with pytest.raises(SandboxFileSystemError):
        sandbox.fs.read_file("missing")
    with pytest.raises(SandboxFileSystemError):
        sandbox.fs.search_files("missing", "*")
    with pytest.raises(ValueError):
        sandbox.fs.create_folder("bad", "999")


def test_text_replacement_preserves_newlines(sandbox):
    sandbox.fs.write_file("crlf.txt", b"one\r\ntwo\r\n")
    sandbox.fs.replace_in_files(["crlf.txt"], "two", "three")
    assert sandbox.fs.read_file("crlf.txt") == b"one\r\nthree\r\n"


def test_upload_failure_preserves_destination_and_removes_temporary_file(
    sandbox, tmp_path
):
    sandbox.fs.write_file("destination", b"original")

    def broken_chunks():
        yield b"partial"
        raise RuntimeError("producer failed")

    with pytest.raises(RuntimeError, match="producer failed"):
        sandbox.fs.upload_file_stream(broken_chunks(), "destination")
    assert sandbox.fs.read_file("destination") == b"original"
    assert not list(tmp_path.glob(".chalk-upload-*"))


def test_download_failure_preserves_local_destination(sandbox, tmp_path):
    destination = tmp_path / "destination"
    destination.write_bytes(b"original")

    def broken_download(*args, **kwargs):
        yield b"partial"
        raise SandboxFileSystemError(errno.EIO, "stream disconnected")

    fs = sandbox.fs
    with patch.object(fs, "download_file_stream", broken_download):
        with pytest.raises(SandboxFileSystemError):
            fs.download_file("remote", destination)
    assert destination.read_bytes() == b"original"
    assert not list(tmp_path.glob(".chalk-download-*"))


def test_streams_are_bounded_and_writes_preserve_permissions(sandbox, tmp_path):
    sandbox.fs.upload_file_stream((b"a" * 4096 for _ in range(100)), "large")
    chunks = list(sandbox.fs.download_file_stream("large"))
    assert max(map(len, chunks)) <= 65536
    assert b"".join(chunks) == b"a" * 409600
    sandbox.fs.set_file_permissions("large", mode="640")
    sandbox.fs.write_file("large", "replacement")
    assert sandbox.fs.get_file_info("large").permissions == "640"
    assert sandbox.fs.read_text("large") == "replacement"
    (tmp_path / "link").symlink_to(tmp_path / "large")
    with pytest.raises(SandboxFileSystemError) as error:
        sandbox.fs.write_file("link", "bad")
    assert error.value.errno == errno.ELOOP
    assert sandbox.fs.read_text("large") == "replacement"
    with pytest.raises(SandboxFileSystemError) as error:
        sandbox.fs.read_file("missing")
    assert error.value.errno == errno.ENOENT


def test_download_iterator_can_be_closed_early(sandbox):
    sandbox.fs.write_file("large", b"a" * 100000)
    stream = sandbox.fs.download_file_stream("large")
    assert len(next(stream)) == 65536
    stream.close()
    assert sandbox.fs.read_file("large") == b"a" * 100000


def frame(value):
    payload = json.dumps(value).encode()
    return struct.pack(">I", len(payload)) + payload


def mocked_process(*chunks):
    process = MagicMock()
    process.stdout_bytes.return_value = (chunk for chunk in chunks)
    return process


def test_protocol_handles_fragmented_and_empty_exec_frames():
    response = frame({"ok": True, "value": {"version": 1, "chunk_size": 65536}})
    process = mocked_process(b"", *(bytes([byte]) for byte in response))
    session = _Session(process)
    session.negotiate()
    assert session.chunk_size == 65536
    session.close()
    process.close_stdin.assert_called_once()


@pytest.mark.parametrize(
    "response",
    [
        struct.pack(">I", 0),
        struct.pack(">I", _MAX_FRAME + 1),
        b"\0\0",
        struct.pack(">I", 1) + b"{",
        frame([]),
        frame({"ok": 1}),
        frame({"ok": True}),
        frame({"ok": False, "errno": "missing", "message": "bad"}),
    ],
)
def test_protocol_rejects_corruption(response):
    with pytest.raises(SandboxFileSystemError) as error:
        _Session(mocked_process(response)).request("work_dir")
    assert error.value.errno == errno.EPROTO


@pytest.mark.parametrize(
    "hello",
    [
        {"version": 2, "chunk_size": 65536},
        {"version": True, "chunk_size": 65536},
        {"version": 1, "chunk_size": 0},
        {"version": 1, "chunk_size": 65537},
    ],
)
def test_protocol_rejects_incompatible_negotiation(hello):
    with pytest.raises(SandboxFileSystemError):
        _Session(mocked_process(frame({"ok": True, "value": hello}))).negotiate()


def test_read_validates_size_before_reading_payload():
    for size in [-1, 65537, True, "10"]:
        with pytest.raises(SandboxFileSystemError):
            _Session(
                mocked_process(frame({"ok": True, "value": {"size": size}}))
            ).read()


def test_server_errno_is_preserved():
    process = mocked_process(
        frame({"ok": False, "errno": errno.ENOENT, "message": "missing"})
    )
    with pytest.raises(SandboxFileSystemError) as error:
        _Session(process).request("stat", path="missing")
    assert error.value.errno == errno.ENOENT


def test_missing_entrypoint_explains_runtime_requirement_and_closes_stdin():
    def failed():
        raise SandboxError("exec: file not found")
        yield

    process = mocked_process()
    process.stdout_bytes.return_value = failed()
    sandbox = Sandbox(sandbox_id="old-runtime")
    sandbox.exec_start = MagicMock(return_value=process)
    with pytest.raises(SandboxFileSystemError, match="supporting runtime"):
        sandbox.fs.read_file("path", timeout_secs=12)
    sandbox.exec_start.assert_called_once_with(
        _ENTRYPOINT, "fs", "--protocol=1", timeout_secs=12
    )
    process.close_stdin.assert_called_once()


def test_transport_failure_cleans_up_and_is_not_replayed():
    def failed():
        yield frame({"ok": True, "value": {"version": 1, "chunk_size": 65536}})
        raise SandboxError("disconnected")

    process = mocked_process()
    process.stdout_bytes.return_value = failed()
    sandbox = Sandbox(sandbox_id="test")
    sandbox.exec_start = MagicMock(return_value=process)
    with pytest.raises(SandboxFileSystemError, match="disconnected"):
        sandbox.fs.read_file("path")
    sandbox.exec_start.assert_called_once()
    process.close_stdin.assert_called_once()


@pytest.mark.parametrize(
    "operation",
    [
        lambda fs: fs.read_file(""),
        lambda fs: fs.read_file("bad\0path"),
        lambda fs: fs.create_folder("path", "999"),
        lambda fs: fs.set_file_permissions("path", owner=True),
        lambda fs: fs.set_file_permissions("path", group=2**32 - 1),
        lambda fs: fs.delete_file("path", recursive="yes"),
    ],
)
def test_invalid_arguments_do_not_start_a_process(operation):
    sandbox = Sandbox(sandbox_id="test")
    sandbox.exec_start = MagicMock()
    with pytest.raises((TypeError, ValueError)):
        operation(sandbox.fs)
    sandbox.exec_start.assert_not_called()


def test_close_releases_response_iterator_when_closing_stdin_fails():
    closed = []

    def output():
        try:
            yield b"x"
        finally:
            closed.append(True)

    process = mocked_process()
    process.stdout_bytes.return_value = output()
    process.close_stdin.side_effect = OSError("stdin disconnected")
    session = _Session(process)
    assert session.read_exact(1) == b"x"
    with pytest.raises(OSError, match="stdin disconnected"):
        session.close()
    assert closed == [True]
    session.close()
    process.close_stdin.assert_called_once()
