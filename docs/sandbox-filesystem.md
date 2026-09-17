# Sandbox filesystem

Use `sandbox.fs` for binary and text files, directories, metadata, permissions,
and search. These helpers use a native entrypoint supplied by the sandbox runtime
at `/.chalk/bin/chalk-sandbox-entrypoint`. They work with custom images without
installing Python, OpenSSH, or extra packages. The runtime change must be deployed
and the sandbox created on that runtime; older sandboxes report an explicit error.

```python
from chalkcompute import Image, SandboxClient

with SandboxClient.from_env() as client:
    sandbox = client.create(image=Image.base("debian:trixie-slim").workdir("/workspace"))
    try:
        sandbox.fs.create_folder("data")
        sandbox.fs.write_file("data/notes.txt", "hello\n")
        print(sandbox.fs.read_text("data/notes.txt"))
        print(sandbox.fs.list_files("data"))
        sandbox.fs.upload_file("local.bin", "data/large.bin")
        sandbox.fs.download_file("data/large.bin", "downloaded.bin")
    finally:
        sandbox.terminate()
```

Paths are relative to the image working directory. `sandbox.get_work_dir()`
returns that directory; `sandbox.get_user_home_dir()` resolves the execution UID
in the sandbox's `/etc/passwd`. Permissions follow the execution user's OS
privileges. Paths must be UTF-8 text; filenames that cannot be represented as
UTF-8 produce an error rather than being silently renamed.

## Transfers and errors

- `upload_file(local_path, remote_path)` and `download_file(remote_path, local_path)`
  stream in bounded chunks. `upload_file_stream(chunks, remote_path)` accepts an
  iterable of bytes; `download_file_stream(remote_path)` yields bytes. Transfers
  use at most 64 KiB of content per protocol request, with no fixed total size cap.
- `read_file`, `read_text`, and `download_file` without a local destination buffer
  the entire result in memory. Downloads to local paths return `None`.
- Uploads write an exclusive adjacent temporary file and atomically rename it only
  after an explicit commit. Parent directories must exist. Existing permissions
  are preserved; new files use 0644. Symlink and non-regular destinations are
  rejected. Replacement files belong to the execution user; ACLs, extended
  attributes, and prior ownership are not copied.
- Downloads to local paths use temporary files and atomic replacement as well.
  Failures before commit preserve the previous destination. A lost upload commit
  response has an unknown outcome: the rename may already have succeeded. The
  SDK never replays a failed stream. A killed helper may leave a temporary file.
- Close a download iterator when stopping early. If a download iterator raises,
  discard any partial content already received. `download_file` handles that
  cleanup when given a local path.
- Remote filesystem errors raise `SandboxFileSystemError` with the server's OS
  errno; protocol and exec failures raise the same exception. Local file/source
  iterator errors propagate to the caller. `timeout_secs` bounds each helper
  process; multi-file search and replacement can use multiple processes.

## Directories, metadata, and editing

`get_file_info` uses lstat semantics; `list_files` returns sorted metadata.
Directory listings use bounded protocol pages, although the returned list is
held in client memory. `create_folder(path, mode="755")` creates missing parents,
subject to the execution process's umask. `move_files(source, destination)` uses
filesystem rename semantics. `delete_file` requires `recursive=True` for
directories and does not traverse directory symlinks.

`set_file_permissions(path, mode="600", owner=1000, group=1000)` accepts optional
octal permissions and numeric UID/GID values and rejects symbolic links.

`search_files(path, "*.txt")` recursively matches filename globs, skipping
symlinks. `find_files(path, "hello")` finds literal text in UTF-8 files, returning
one-based line numbers; invalid UTF-8 content is skipped.
`replace_in_files(paths, "hello", "goodbye")` returns replacement counts and
preserves line endings. Text operations buffer one candidate file at a time.
Replacement is atomic per file, not across the group; coordinate concurrent edits
externally.
