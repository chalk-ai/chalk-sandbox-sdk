# Sandbox capabilities and defaults

| Surface | Supported API |
| --- | --- |
| Git | `sandbox.git`: clone/init, staging/commit, status, branches, fetch/pull/push, reset/restore, remotes, local config and identity. |
| Files | `sandbox.fs`: native SFTP upload/download/read/write, directories, metadata/listing, permissions/ownership, filename glob search, literal text search/replacement. |
| Processes | `exec`, `exec_stream`, `exec_start`, stdin, signals, reconnectable buffered output, and checked raw-byte stdout streaming. Container sessions separately expose PTYs and session discovery. |
| Code execution | Stateful `execute_python` with sessions and artifacts. Other installed runtimes can be invoked with `exec`. |
| Directories | `get_work_dir()` and `get_user_home_dir()` work on newly created and attached handles with SFTP support. |
| System tools | `Image.sandbox().apt_install(["tmux"])` installs tools at build time. Detached tmux sessions are managed with ordinary exec calls. |
| Resources | Creation-time CPU/memory/GPU, env/Secret references, volume mounts, lifetime, tags, network policy, published port and managed outbound SSH. |

## Git defaults

Clones use the remote's default branch with **depth 1**. Pass `depth=None` for
full history or a positive integer for a different depth. `commit_id` fetches the
selected commit and checks it out as detached HEAD, including for shallow clones.
HTTPS credentials are scoped to each command and are never written into Git
config. Pulls use fast-forward only, reset defaults to mixed/HEAD, and branch
deletion defaults to merged branches. Config changes are repository-local.

## Filesystem transport

Filesystem operations speak SFTP to the native OpenSSH `sftp-server` binary over
Chalk's authenticated bidirectional exec connection. There is no remote Python,
shell-script dispatch, SSH daemon, exposed port, or additional authentication.
Paths are passed as binary protocol fields, not interpolated into commands. Each
operation starts a server process and closes it after the operation completes.

The default image includes `/usr/lib/openssh/sftp-server`. Custom Debian/Ubuntu
images can add `.apt_install(["openssh-sftp-server"])`. For a different binary
location, use `SandboxFileSystem(sandbox, server_path="/path/to/sftp-server")`.
File operations do not require a Python runtime inside the image.

- `upload_file(local_path, remote_path)` and `download_file(remote_path, local_path)`
  stream in bounded chunks. `upload_file_stream` and `download_file_stream` accept
  or yield byte chunks. No fixed maximum file size is imposed by the SDK.
- `read_file` and `read_text` return the whole contents in memory. A download with
  no local destination also returns bytes; a download to a local path returns None.
- Whole-file uploads/writes use an exclusive temporary file next to the destination
  and atomic rename after all writes are acknowledged. Failed transfers never
  replace the destination with partial data. Temporary files are cleaned up when
  the connection remains usable; a lost connection may leave an orphan temporary.
- Existing file modes are preserved; new files use 0644. Replacements have the
  execution user's ownership. Symlink write destinations are rejected. Downloads
  to local paths also use temporary files and atomic replacement.
- Streams are not restarted after disconnects. Consumers of a download iterator
  must discard partial output after an error and close the iterator on early exit.
- File errors raise `SandboxFileSystemError`, retaining the server errno where
  available. Permissions follow the execution user's OS privileges; ownership
  setters accept numeric UID/GID values.
- Recursive search skips symlinks. Text search/replacement runs on the client and
  reads one file into memory at a time. Invalid UTF-8 files are skipped by search.
  Replacement preserves line endings and is atomic per file, not across the group.
  Callers should coordinate concurrent edits to files they replace.

## Image and lifecycle defaults

Omitting the image uses `Image.sandbox()`: Python 3.14 slim, Git, CA certificates,
the OpenSSH client and native SFTP server, and `/workspace` as the working directory. Explicit
images are unchanged. The image builds through the standard image builder.
`apt_install([...])` returns a new image and requires apt-get and a root build user.
It installs noninteractively without recommended packages and cleans up indexes.

CPU/memory/GPU, entrypoint, lifetime, and network defaults remain server-owned.
Creation waits for readiness by default with a 600-second timeout and 0.5-second
polling. Helper `timeout_secs=None` leaves the exec timeout unset. An SFTP timeout
applies to the entire operation's server process; a multi-command Git operation
applies it to each subprocess. tmux sessions end when the sandbox terminates;
interactive attachment requires a PTY.
