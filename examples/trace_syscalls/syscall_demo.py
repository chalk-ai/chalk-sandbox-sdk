#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = ["chalkcompute>=2.15.0"]
# ///
"""Trace system calls from a command in a Chalk sandbox and print a waterfall.

The sample workload reads and writes temporary files and attempts a loopback
connection that is deliberately refused. The waterfall shows when each call ran,
how long it took, and what it returned.

This example requires standard Chalk credentials with permission to create
and terminate sandboxes and read spans. It creates one billable sandbox and
terminates it on exit. Chalk retains the spans for inspection by trace ID.
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep

from chalkcompute import Sandbox, SandboxClient, Span, TraceClient

# Set the image, command, and syscall allowlist to trace a different workload.
IMAGE = "python:3.12-slim"
WORKLOAD_PATH = "/tmp/trace_syscalls_workload.py"
SAMPLE_COMMAND = ("python3", "-S", WORKLOAD_PATH)
COMMAND = SAMPLE_COMMAND
SYSCALLS = ("openat", "read", "write", "socket", "connect")

# Syscall spans report Linux errno values.
LINUX_ECONNREFUSED = 111


def show_waterfall(
    spans: list[tuple[Span, datetime, datetime]], *, sample_completed: bool
) -> None:
    origin = spans[0][1]
    total = max((end - origin).total_seconds() for _, _, end in spans)
    scale = 32 / max(total, 0.000001)
    name_width = max(
        len("Syscall"), *(len(span.operation_name) for span, _, _ in spans)
    )
    color = sys.stdout.isatty()
    print(
        f"\n  {'Syscall':<{name_width}}  {'Start':>10}  {'Duration':>10}  {'Timeline':32}  Result"
    )
    for span, start, end in spans:
        attrs = span.attributes
        offset = (start - origin).total_seconds()
        duration = (end - start).total_seconds()
        left = min(31, int(offset * scale))
        width = min(32 - left, max(1, round(duration * scale)))
        error = int(attrs.get("syscall.errno", "0"))
        expected_refusal = (
            sample_completed
            and span.operation_name == "syscall.connect"
            and error == LINUX_ECONNREFUSED
        )
        bar = " " * left + "━" * width + " " * (32 - left - width)
        if color:
            bar = f"\033[{31 if error and not expected_refusal else 36}m{bar}\033[0m"
        if error:
            result = "ECONNREFUSED" if error == LINUX_ECONNREFUSED else f"errno {error}"
            if expected_refusal:
                result += " (expected)"
            elif color:
                result = f"\033[31;1m{result}\033[0m"
        elif "syscall.transferred_bytes" in attrs:
            count = attrs["syscall.transferred_bytes"]
            requested = attrs.get("syscall.requested_bytes")
            result = f"{count} / {requested} bytes" if requested else f"{count} bytes"
        elif "syscall.result_fd" in attrs:
            result = f"fd {attrs['syscall.result_fd']}"
        else:
            result = attrs.get("syscall.result", "")
        print(
            f"  {span.operation_name:<{name_width}}  {offset * 1e3:7.3f} ms"
            f"  {duration * 1e6:7.0f} µs  {bar}  {result}"
        )


def show_syscall_trace(
    client: TraceClient,
    sandbox_id: str,
    started_at: datetime,
    *,
    sample_completed: bool,
) -> None:
    print("Waiting for Chalk to index the spans…", flush=True)
    span_limit = 1000

    # Spans arrive asynchronously. The sample ends with a refused connection.
    # For a custom workload, wait until the returned span IDs stop changing.
    deadline = monotonic() + 30
    last_change = monotonic()
    seen: set[str] = set()
    while True:
        recorded = client.list_sandbox_spans(
            sandbox_id, start_time=started_at, limit=span_limit
        )
        spans = [
            (span, start, end)
            for span in recorded
            if span.operation_name.startswith("syscall.")
            and (start := span.start_time) is not None
            and (end := span.end_time) is not None
        ]
        current = {span.span_id for span, _, _ in spans}
        now = monotonic()
        if current != seen:
            seen = current
            last_change = now
        refused = any(
            span.operation_name == "syscall.connect"
            and span.attributes.get("syscall.errno") == str(LINUX_ECONNREFUSED)
            for span, _, _ in spans
        )
        if now >= deadline or (
            spans and (refused if sample_completed else now - last_change >= 3)
        ):
            break
        sleep(1)

    if not spans:
        raise RuntimeError(f"No syscall spans found for sandbox {sandbox_id}")
    spans.sort(key=lambda entry: entry[1])

    if sample_completed:
        # The short read identifies the sample's file operations amid Python
        # startup and sample setup. Show from its open through the connect.
        preceding_open = None
        for index, (span, _, _) in enumerate(spans):
            if span.operation_name == "syscall.openat":
                preceding_open = index
            if (
                span.operation_name == "syscall.read"
                and span.attributes.get("syscall.requested_bytes") == "128"
                and span.attributes.get("syscall.transferred_bytes") == "96"
            ):
                first = index if preceding_open is None else preceding_open
                last = next(
                    (
                        i + 1
                        for i in range(index, len(spans))
                        if spans[i][0].operation_name == "syscall.connect"
                    ),
                    len(spans),
                )
                spans = spans[first:last]
                break
        if not refused:
            print("The refused connection span did not arrive before the deadline.")

    if len(recorded) == span_limit:
        print(f"The span query reached its limit of {span_limit} spans.")
    if len(spans) > 40:
        print(f"Showing the 40 most recent of {len(spans)} syscall spans.")
    shown = spans[-40:]
    show_waterfall(shown, sample_completed=sample_completed)
    print(
        "\n  Chalk trace IDs "
        + ", ".join(dict.fromkeys(span.trace_id for span, _, _ in shown))
    )


def main() -> None:
    sandbox = Sandbox(
        image=IMAGE,
        trace_syscalls=SYSCALLS,
        # Cap billable runtime if the local process cannot reach cleanup.
        lifetime="300s",
    )
    with TraceClient() as trace_client, SandboxClient.from_env() as client:
        try:
            # Allow a few seconds of clock skew in the server-side span query.
            started_at = datetime.now(UTC) - timedelta(seconds=5)
            print("Starting a sandbox with syscall tracing…", flush=True)
            # Leave time for both command execution and span indexing within the lifetime.
            client.run(sandbox, timeout=120)
            sandbox_id = sandbox.id
            assert sandbox_id is not None
            sample_workload = COMMAND == SAMPLE_COMMAND
            if sample_workload:
                sandbox.fs.upload_file(
                    Path(__file__).with_name("workload.py"), WORKLOAD_PATH
                )

            print(f"Sandbox {sandbox_id} · running {COMMAND[0]}", flush=True)

            result = sandbox.exec(*COMMAND, timeout_secs=90)
            if result.stderr_text:
                print(result.stderr_text.rstrip(), file=sys.stderr)
            if result.stdout_text:
                print(result.stdout_text.rstrip())

            # Display any captured spans even when the command fails.
            show_syscall_trace(
                trace_client,
                sandbox_id,
                started_at,
                sample_completed=sample_workload and result.exit_code == 0,
            )
            if result.exit_code != 0:
                raise SystemExit(f"Workload exited with status {result.exit_code}")
        finally:
            if sandbox.id is not None:
                sandbox.terminate()


if __name__ == "__main__":
    main()
