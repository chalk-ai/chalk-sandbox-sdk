"""Formatting plumbing for the bash tool: stdout + [stderr] + exit code, capped
at 16KB. What the model reads — distinct from `ui.tool_output`, which is what
the viewer reads."""

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


def format_result(r) -> str:
    parts = []
    if r.stdout:
        parts.append(_trim(r.stdout))
    if r.stderr:
        parts.append(f"[stderr]\n{_trim(r.stderr)}")
    parts.append(f"[exit {r.exit_code}]")
    return "\n".join(parts)
