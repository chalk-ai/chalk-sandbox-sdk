"""Function-tool wrappers around a long-lived ``chalkcompute.Container``.

The agent gets three tools — a shell, a file reader, and a workspace lister —
all backed by ``container.exec`` against a single sandbox. The container is
created once per investigation, holds the case packet on a mounted Volume,
and is torn down at the end.

This is the lighter integration style: we expose Chalk Compute as
``@function_tool`` callables to a regular ``agents.Agent``, rather than
implementing the OpenAI Agents SDK's ``BaseSandboxClient`` provider contract.
The visible behavior to a viewer recording the demo is the same.
"""

from __future__ import annotations

from agents import function_tool

from chalkcompute import Container

# The agent always operates inside this directory. Match it across the system
# prompt and the tools so paths the agent emits are relative to it.
WORKSPACE = "/workspace"

# Output truncation cap for shell commands. Long outputs blow up the context
# window without adding signal; the agent can re-run with `head` or `wc -l`
# if it actually needs more.
_MAX_OUTPUT_BYTES = 16 * 1024


def _trim(blob: bytes) -> str:
    if len(blob) <= _MAX_OUTPUT_BYTES:
        return blob.decode("utf-8", errors="replace")
    head = blob[: _MAX_OUTPUT_BYTES // 2]
    tail = blob[-_MAX_OUTPUT_BYTES // 2 :]
    omitted = len(blob) - len(head) - len(tail)
    return (
        head.decode("utf-8", errors="replace")
        + f"\n... [{omitted} bytes omitted] ...\n"
        + tail.decode("utf-8", errors="replace")
    )


def build_tools(container: Container) -> list:
    """Return a list of `@function_tool`s bound to ``container``.

    Each tool closes over the live Container handle, so the agent's shell
    state (working directory, files written) persists across tool calls
    within a single investigation.
    """

    @function_tool
    def chalk_shell(command: str) -> str:
        """Run a bash command inside the Chalk sandbox.

        Args:
            command: A single bash command string. The default working
                directory is /workspace. Use absolute paths for files
                outside the workspace (e.g. /opt/tools/ocr.py).

        Returns:
            Combined stdout/stderr with the trailing exit code. Output
            longer than 16KB is truncated with the middle elided.
        """
        result = container.exec("bash", "-lc", f"cd {WORKSPACE} && {command}")
        out = _trim(result.stdout)
        err = _trim(result.stderr)
        chunks = []
        if out:
            chunks.append(out)
        if err:
            chunks.append(f"[stderr]\n{err}")
        chunks.append(f"[exit {result.exit_code}]")
        return "\n".join(chunks)

    @function_tool
    def chalk_read_file(path: str) -> str:
        """Read a UTF-8 text file from the sandbox.

        Args:
            path: Absolute path or workspace-relative path.

        Returns:
            File contents (truncated if very large), or an error string.
        """
        target = path if path.startswith("/") else f"{WORKSPACE}/{path}"
        result = container.exec("cat", "--", target)
        if result.exit_code != 0:
            return f"[error reading {path}: {result.stderr_text.strip()}]"
        return _trim(result.stdout)

    @function_tool
    def chalk_list_workspace() -> str:
        """List files in the workspace, with sizes."""
        result = container.exec("ls", "-la", WORKSPACE)
        return _trim(result.stdout) or result.stderr_text

    return [chalk_shell, chalk_read_file, chalk_list_workspace]
