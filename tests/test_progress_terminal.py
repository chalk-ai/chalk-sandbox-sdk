"""Exercise real PTYs as well as non-interactive fallbacks in fresh processes."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal tests")


def _run_terminal(code: str, *, env=None, stdout_terminal=False, width=80, height=24):
    import errno
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
    environment = os.environ.copy()
    for name in (
        "CI",
        "BUILDKITE",
        "GITHUB_ACTIONS",
        "TF_BUILD",
        "NO_COLOR",
        "CHALK_SANDBOX_TUI",
        "FORCE_COLOR",
        "TTY_COMPATIBLE",
        "TTY_INTERACTIVE",
        "COLUMNS",
        "LINES",
    ):
        environment.pop(name, None)
    environment.update(TERM="xterm-256color", PYTHONIOENCODING="utf-8")
    environment.update(env or {})

    def read_terminal():
        chunks = []
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")

    process = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(code)],
        stdout=slave if stdout_terminal else subprocess.PIPE,
        stderr=slave,
        env=environment,
    )
    os.close(slave)
    try:
        with ThreadPoolExecutor() as pool:
            reader = pool.submit(read_terminal)
            try:
                stdout, _ = process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            terminal = reader.result(timeout=5)
        assert process.returncode == 0, terminal
        return (stdout or b"").decode(), terminal.replace("\r\n", "\n")
    finally:
        os.close(master)


def test_live_display_updates_concurrent_rows_and_restores_streams():
    stdout, output = _run_terminal("""
        import sys
        from chalkcompute._progress import DeployProgress
        original = (sys.stdout, sys.stderr)
        p = DeployProgress()
        p.add("a", "function", 'Function "A"')
        p.add("b", "function", 'Function "B"')
        assert (sys.stdout, sys.stderr) == original
        p.add("reg:a", "function", "Registering function", parent="a")
        assert p._display is not None
        assert sys.stdout is original[0]  # Piped stdout must stay machine-readable.
        p.add("reg:b", "function", "Registering function", parent="b")
        p.update("reg:a", status="done", label="Function registered")
        p.update("a", detail="Pending ready=0 available=0 — waiting for startup probe")
        p.update("b", status="done", detail="ready")
        p.finish_if_idle()
        assert p._display is not None
        p.update("a", status="done", detail="ready")
        p.finish_if_idle()
        assert (sys.stdout, sys.stderr) == original
        print('{"result": 42}')
    """)
    assert stdout == '{"result": 42}\n'
    assert "\x1b[2K" in output
    assert (
        "\x1b[2J" not in output
    )  # Never clear the screen or enter an alternate screen.
    assert "1049" not in output
    assert "(continued)" not in output
    assert "startup probe" in output
    assert output.endswith("\x1b[?25h")
    assert "Ready" in output


@pytest.mark.parametrize(
    "configuration",
    [
        'os.environ["CI"] = "1"',
        'os.environ["BUILDKITE"] = "true"',
        'os.environ["GITHUB_ACTIONS"] = "true"',
        'os.environ["TF_BUILD"] = "True"',
        'os.environ["TERM"] = "dumb"',
        'os.environ["TERM"] = "unknown"',
        'os.environ["TTY_INTERACTIVE"] = "0"',
        'os.environ["TTY_COMPATIBLE"] = "0"',
        'os.environ.pop("TERM", None)',
        'sys.modules["ipykernel"] = object()',
        'os.environ["CHALK_SANDBOX_TUI"] = "plain"',
        "logging.basicConfig()",
        "import chalkcompute; chalkcompute.configure_logging()",
    ],
)
def test_unsafe_environments_never_rewrite_even_with_a_pty(configuration):
    _, output = _run_terminal(f"""
        import os, sys, logging
        from chalkcompute._progress import DeployProgress
        {configuration}
        original = (sys.stdout, sys.stderr)
        p = DeployProgress()
        p.add("fn", "function", "Fraud")
        p.update("fn", detail="Pending")
        assert p._display is None
        for _ in range(10):
            p.update("fn", detail="Pending")
        p.update("fn", status="done", detail="ready")
        p.finish()
        assert (sys.stdout, sys.stderr) == original
    """)
    assert "\x1b[2K" not in output
    assert "\r" not in output
    assert output.count("Pending") == 1
    assert output.count("Ready") == 1


@pytest.mark.parametrize("stdout_terminal", [False, True])
def test_customer_output_and_warnings_are_preserved_verbatim(stdout_terminal):
    stdout, output = _run_terminal(
        """
        import sys, logging
        from chalkcompute._progress import DeployProgress
        original = (sys.stdout, sys.stderr)
        p = DeployProgress()
        p.add("fn", "function", "Fraud", detail="Pending")
        print("[bold]customer stdout[/bold]")
        print("[bold]customer stderr[/bold]", file=sys.stderr)
        logging.getLogger("app").warning("application warning")
        sys.stderr.writelines(["customer writelines", " still intact\\n"])
        p.update("fn", detail="Waiting for readiness")
        print("partial", end="", file=sys.stderr, flush=True)
        p.update("fn", detail="Still waiting")
        print(" line", file=sys.stderr)
        p.update("fn", status="failed", detail="startup failed")
        p.finish()
        assert (sys.stdout, sys.stderr) == original
    """,
        stdout_terminal=stdout_terminal,
    )
    assert (output if stdout_terminal else stdout).count(
        "[bold]customer stdout[/bold]\n"
    ) == 1
    assert output.count("[bold]customer stderr[/bold]\n") == 1
    assert output.count("application warning\n") == 1
    assert "customer writelines still intact\n" in output
    assert "partial line\n" in output
    assert "Failed" in output
    assert output.endswith("\x1b[?25h")


def test_live_no_color_and_ascii_encoding():
    _, output = _run_terminal(
        """
        import sys
        from chalkcompute._progress import DeployProgress
        sys.stderr.reconfigure(encoding="ascii")
        p = DeployProgress()
        p.add("fn", "function", "Fraud", detail="Pending")
        assert p._display is not None
        p.update("fn", status="done", detail="ready")
        p.finish()
    """,
        env={"NO_COLOR": "1"},
    )
    assert "+ Fraud: Ready" in output
    assert "\x1b[32m" not in output
    assert "\x1b[1m" not in output
    assert "\x1b[2K" in output


def test_redirecting_stderr_during_deployment_restores_only_owned_streams():
    _, output = _run_terminal("""
        import contextlib, io, sys
        from chalkcompute._progress import DeployProgress
        original = sys.stderr
        p = DeployProgress()
        p.add("fn", "function", "Fraud", detail="Pending")
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            p.update("fn", detail="Redirected")
            p.update("fn", status="done", detail="ready")
            p.finish()
            assert sys.stderr is captured
        # contextlib may restore the old proxy; it must now be pass-through.
        print("after capture", file=sys.stderr)
        assert "Redirected" in captured.getvalue()
        assert "Ready" in captured.getvalue()
        assert "\\x1b" not in captured.getvalue()
        sys.stderr = original
    """)
    assert "after capture" in output


def test_atexit_leaves_a_final_snapshot_and_visible_cursor():
    _, output = _run_terminal("""
        from chalkcompute._progress import progress_add
        progress_add("fn", "function", "Fraud", detail="Pending")
    """)
    assert "Pending" in output
    assert output.endswith("\x1b[?25h")


def test_narrow_short_terminal_keeps_all_final_results():
    _, output = _run_terminal(
        """
        from chalkcompute._progress import DeployProgress
        p = DeployProgress()
        for i in range(6):
            p.add(str(i), "function", f"Function {i}", detail="Waiting for readiness")
        for i in range(6):
            p.update(str(i), status="done", detail="ready")
        p.finish()
    """,
        width=28,
        height=4,
        env={"NO_COLOR": "1"},
    )
    # The final render expands beyond the live viewport so no result is lost.
    final = output[output.rfind("\x1b[2K") + len("\x1b[2K") :]
    for i in range(6):
        assert f"Function {i}: Ready" in final


def test_logging_handler_created_during_live_output_survives_reuse():
    _, output = _run_terminal("""
        import sys, logging
        from chalkcompute._progress import DeployProgress
        original = sys.stderr
        p = DeployProgress()
        p.add("a", "function", "A", detail="Pending")
        logger = logging.getLogger("application")
        handler = logging.StreamHandler(sys.stderr)
        logger.addHandler(handler)
        logger.propagate = False
        logger.warning("first warning")
        p.update("a", status="done", detail="ready")
        p.finish()
        assert sys.stderr is original
        p.add("b", "function", "B", detail="Pending")
        assert p._display is None  # Handler retained a proxy for the old display.
        logger.warning("second warning")
        p.update("b", status="done", detail="ready")
        p.finish()
        assert logger.handlers == [handler]
    """)
    assert output.count("first warning\n") == 1
    assert output.count("second warning\n") == 1


def test_live_renderer_failure_restores_streams_and_allows_plain_results():
    _, output = _run_terminal("""
        import sys
        from rich.live import Live
        from chalkcompute._progress import DeployProgress
        original = (sys.stdout, sys.stderr)
        p = DeployProgress()
        p.add("a", "function", "A", detail="Pending")
        def fail(*args, **kwargs):
            raise OSError("terminal went away")
        Live.update = fail
        p.update("a", detail="Waiting")
        assert (sys.stdout, sys.stderr) == original
        p.update("a", status="done", detail="ready")
        p.finish()
    """)
    assert "Ready" in output


def test_parallel_deployments_and_application_output_do_not_deadlock():
    _, output = _run_terminal("""
        import sys
        from concurrent.futures import ThreadPoolExecutor
        from chalkcompute._progress import DeployProgress
        original = (sys.stdout, sys.stderr)
        p = DeployProgress()
        for i in range(4):
            p.add(str(i), "function", f"Function {i}", detail="Pending")
        def deploy(i):
            for step in range(3):
                p.update(str(i), detail=f"Step {step}")
                sys.stderr.write(f"application {i}:{step}\\n")
            p.update(str(i), status="done", detail="ready")
            p.finish_if_idle()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(deploy, range(4)))
        assert (sys.stdout, sys.stderr) == original
    """)
    for i in range(4):
        for step in range(3):
            assert output.count(f"application {i}:{step}\n") == 1
    assert output.endswith("\x1b[?25h")
