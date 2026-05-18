"""The agent's one tool: bash. Bound to a long-lived Chalk Compute Container.

The agent-facing format (stdout + [stderr] + exit code, capped at 16KB) is what
the model reads — distinct from `ui.tool_output`, which is what the *viewer*
reads. Same data, different audience.
"""

from agents import function_tool

from chalkcompute import Container

WORKSPACE = "/workspace"
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


def _format_result(r) -> str:
    parts = []
    if r.stdout:
        parts.append(_trim(r.stdout))
    if r.stderr:
        parts.append(f"[stderr]\n{_trim(r.stderr)}")
    parts.append(f"[exit {r.exit_code}]")
    return "\n".join(parts)


def make_bash_tool(container: Container):
    """Return a `bash` @function_tool bound to ``container``."""

    @function_tool
    def bash(command: str) -> str:
        """Run a bash command in the sandbox. cwd=/workspace."""
        return _format_result(container.exec("bash", "-lc", f"cd {WORKSPACE} && {command}"))

    return bash
