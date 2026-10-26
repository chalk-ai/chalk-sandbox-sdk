# Files inside a sandbox

`sandbox.fs` exposes `SandboxFileSystem` methods for moving files in and out of
a sandbox and managing them in place. Transfers use the sandbox filesystem RPC.
Directory, search, and permission methods run GNU coreutils, findutils, and
grep inside the sandbox; the default `Image.sandbox()` includes them, and custom
images need them too (BusyBox-only images are not supported).

`sandbox.fs`, `sandbox.git`, and `exec` need a `HOST` compute class sandbox (the
default). On a `ComputeClass.K8S` sandbox they raise `SandboxError` without
contacting the server.

## Declare files before the sandbox starts

`add_file` works on a sandbox that has not started yet. Queued files are uploaded
by `run()` as soon as the sandbox is ready, after its entrypoint starts.
`run(wait=False)` is rejected when files are queued. If a queued upload fails,
`run()` terminates the sandbox and re-raises; the files stay queued, so `run()`
can be retried. On a running sandbox, `add_file` uploads immediately.

```python
from chalkcompute import Sandbox

sandbox = Sandbox()
sandbox.fs.add_file("config.toml", "/workspace/config.toml", mode=0o600)
sandbox.fs.add_file(b"#!/bin/sh\necho ready\n", "/workspace/run.sh", mode=0o755)
sandbox.run()
print(sandbox.exec("/workspace/run.sh").stdout_text)
```

A local path given to `add_file` must exist when it is added, and is read when
it is uploaded.

## Upload and download

```python
from chalkcompute import FileDownload, FileUpload

sandbox.fs.upload_file("local.csv", "/workspace/data.csv")   # local path
sandbox.fs.upload_file(b"hello\n", "/workspace/hello.txt")    # bytes
sandbox.fs.upload_files([
    FileUpload(b"print('hi')\n", "/workspace/app/main.py"),
    FileUpload("model.bin", "/workspace/app/model.bin", mode=0o600),
])

contents = sandbox.fs.download_file("/workspace/hello.txt")   # returns bytes
sandbox.fs.download_file("/workspace/data.csv", "copy.csv")   # writes a local file
results = sandbox.fs.download_files([
    FileDownload("/workspace/hello.txt"),
    FileDownload("/workspace/data.csv", "copy.csv"),
])  # [b"hello\n", None]
```

Uploads, including `write_bytes`, create missing parent directories. Local downloads atomically replace
the destination. For large files, `upload_iter` and `download_iter` stream byte
chunks; `write_bytes` and `read_bytes` transfer whole files in memory.

## Method reference

| Method | Behavior |
| --- | --- |
| `add_file(source, path, mode=None)` | Upload bytes or a local file now, or queue it until `run()`. |
| `upload_file(source, path, mode=None)` | Upload bytes or a local file. |
| `upload_files(files)` | Upload `FileUpload` values in order; stops at the first failure. |
| `download_file(path, local_path=None)` | Return bytes, or write `local_path` and return `None`. |
| `download_files(files)` | Download `FileDownload` values in order. |
| `list_files(path=".")` | Directory entries as `SandboxFileInfo`, sorted by name, not recursive. |
| `get_file_info(path)` | `SandboxFileInfo` for one path; a final symlink is not followed. |
| `create_folder(path, mode=0o755)` | `mkdir -p`; existing directories are kept. |
| `delete_file(path, recursive=False)` | Delete a file, symlink, or empty directory; `recursive=True` deletes a tree. |
| `move_files(source, destination)` | Rename with `mv` semantics. |
| `set_file_permissions(path, mode=, owner=, group=)` | `chmod` and/or `chown`. |
| `search_files(path, pattern)` | Recursively match names against a glob such as `*.py`; returns paths. |
| `find_files(path, pattern, regex=False)` | Recursively search text contents; returns `FileMatch(file, line, content)`. |
| `replace_in_files(files, pattern, new_value)` | Literal replacement; returns a `ReplaceResult` per file. |

`SandboxFileInfo` has `name`, `path`, `size`, `mode` (permission bits as an
int), `mod_time` (UTC `datetime`), `is_dir`, `is_symlink`, `owner`, `group`, and
`symlink_target`.

Modes are integers such as `0o755`. Every method accepts `timeout_secs`.
Failures raise `SandboxFileSystemError`, whose `error` is a `FilesystemError`
such as `NOT_FOUND`, `PERMISSION_DENIED`, or `FAILED_PRECONDITION`.

`replace_in_files` downloads each file, replaces the text locally, and uploads
it with its original permission bits only if it changed. A failure for one file
is recorded in its result and does not stop the others.
