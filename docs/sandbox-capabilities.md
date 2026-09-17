# Sandbox capabilities and defaults

Reviewed Daytona's public Python SDK on 2026-09-16. The goal is equivalent
capabilities using Chalk's existing exec transport, rather than an API-compatible
Daytona adapter.

| Surface | Chalk support after this change |
| --- | --- |
| Git | `sandbox.git`: clone/init, staging/commit, status, branches, fetch/pull/push, reset/restore, remotes, local config and identity. No persistent credential-storage helper or TLS-verification bypass. |
| Files | `sandbox.fs`: binary upload/download/read/write, mkdir/delete/rename, metadata/listing, permissions/ownership, filename glob search, literal text search/replacement. Uses Python 3 in the sandbox; no new service required. |
| Processes | Existing `exec`, `exec_stream`, `exec_start`, stdin, signals, and reconnectable output. Container sessions separately expose PTYs and session discovery. No redundant `process` alias. |
| Code execution | Existing stateful `execute_python` with sessions and artifacts. Other installed runtimes can be invoked with `exec`. |
| Directories | New `get_work_dir()` and `get_user_home_dir()` work on newly created and attached handles. |
| Creation | New optional image: `Image.sandbox()` is a fresh, extensible Python/Git image spec. Existing explicit-image and attached-handle paths remain unchanged. |
| Resources, env, secrets, storage, network, SSH | Existing creation-time CPU/memory/GPU, env/Secret references, volume mounts, lifetime, tags, network policy, published port and managed outbound SSH. |

Daytona's [Git reference](https://www.daytona.io/docs/en/python-sdk/sync/git/)
was used to review clone defaults and repository operations. Chalk clones the
remote's default branch with full history, unless `branch`/`depth` are supplied.
A selected `commit_id` produces detached HEAD and must be available in the fetched
history. Credentials use a command-scoped helper. Pulls use fast-forward only,
reset defaults to mixed/HEAD, and branch deletion defaults to merged branches.
Return values are Chalk dataclasses, strings, and lists.

The [filesystem reference](https://www.daytona.io/docs/en/python-sdk/sync/file-system/)
also offers bulk and streaming transfers. Chalk's new transfers are buffered,
with chunked stdin uploads; true bounded-memory download streaming needs a raw
byte stream interface (the existing `ExecProcess.output()` emits decoded text).
Downloads retry a hypervisor `RST_STREAM` error at most twice, discarding partial
output; uploads fail on transport disconnect rather than reattaching with lost
stdin. Use volumes for large files. Search/replacement use literal text, filename search
uses globs, and recursive searches skip symlinks and invalid UTF-8 files. Deletes
require an explicit recursive flag for directories. A replacement across several
files is not transactional.

## Defaults

[Daytona creation](https://www.daytona.io/docs/en/python-sdk/sync/daytona/)
selects its default snapshot and Python when omitted. Its
[sandbox guide](https://www.daytona.io/docs/sandboxes) documents a default size of
1 vCPU, 1 GB RAM, and 3 GiB disk, plus a 15-minute inactivity stop for container
sandboxes. These are platform policies, not portable SDK-only defaults.

Chalk's omitted image builds Python 3.14 slim with Git, CA certificates, and
OpenSSH, working in `/workspace`. Custom images are untouched. CPU/memory/GPU,
entrypoint, lifetime, and network defaults remain server-owned. Creation still
waits for readiness by default with a 600-second timeout and 0.5-second polling.
Git/file helper `timeout_secs=None` leaves the exec timeout unset. A multi-command
operation applies a supplied timeout to each subprocess, not the whole operation.
No idle-stop policy is added or simulated by a client-side timer.

## Remaining platform work

The [Daytona Sandbox surface](https://www.daytona.io/docs/en/python-sdk/sync/sandbox/)
includes lifecycle pause/archive/recovery, forks/snapshots, resizing, activity and
auto-stop controls, metrics, signed preview URLs, live env/secret/network updates,
inbound SSH tokens, LSP, and desktop automation. These need dedicated backend
contracts/runtime support; they are not implemented by this SDK change. Chalk's
generated service contains suspend/resume RPC names, but its status enum has no
suspended state, so this change does not promise a functioning pause lifecycle.

[Daytona processes](https://www.daytona.io/docs/en/python-sdk/sync/process/)
include named sessions, entrypoint logs, and PTY management. Extending the sandbox
process service and exposing raw streaming transfers are follow-up platform tasks;
existing Chalk exec and Python execution remain the supported interfaces.
