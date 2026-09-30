# Sandbox capabilities and defaults

| Surface | Supported API |
| --- | --- |
| Git | `sandbox.git`: clone/init, staging/commit, status, branches, fetch/pull/push, reset/restore, remotes, local config and identity. |
| Processes | `exec`, `exec_stream`, `exec_start`, stdin, signals, reconnectable buffered output. Container sessions separately expose PTYs and session discovery. |
| Code execution | Stateful `execute_python` with sessions and artifacts. Other installed runtimes can be invoked with `exec`. |
| System tools | `Image.sandbox().apt_install(["tmux"])` installs tools at build time. Detached tmux sessions are managed with ordinary exec calls. |
| Resources | Creation-time CPU/memory/GPU, env/Secret references, volume mounts, lifetime, tags, network policy, published port and managed outbound SSH. |
| Tracing | `tracing=True` / `TracingPolicy(sample_rate=...)` / `False` at creation; the caller's trace context rides every RPC and reaches each `exec` as `TRACEPARENT`. |

## Git defaults

See the [sandbox Git guide](sandbox-git.md) for examples and the method reference.

Clones use the remote's default branch with **depth 1**. Pass `depth=None` for
full history or a positive integer for a different depth. `commit_id` fetches the
selected commit and checks it out as detached HEAD, including for shallow clones.
HTTPS credentials are scoped to each command and are never written into Git
config. Pulls use fast-forward only, reset defaults to mixed/HEAD, and branch
deletion defaults to merged branches. Config changes are repository-local.

## Image and lifecycle defaults

Omitting the image uses `Image.sandbox()`: Python 3.14 slim, Git, CA certificates,
the OpenSSH client, and `/workspace` as the working directory. Explicit
images are unchanged. The image builds through the standard image builder.
`apt_install([...])` returns a new image and requires apt-get and a root build user.
It installs noninteractively without recommended packages and cleans up indexes.

CPU/memory/GPU, entrypoint, lifetime, and network defaults remain server-owned.
Creation waits for readiness by default with a 600-second timeout and 0.5-second
polling. Helper `timeout_secs=None` leaves the exec timeout unset; a multi-command
Git operation applies it to each subprocess. tmux sessions end when the sandbox terminates;
interactive attachment requires a PTY.

## Tracing defaults

The platform gives every sandbox an OTLP exporter endpoint and a default
parent-based sampler; those stay in effect unless `tracing=` is passed.
`tracing=True` traces every root inside the sandbox, `TracingPolicy(sample_rate=...)`
sets the root sample rate, and `False` turns tracing off.
