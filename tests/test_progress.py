from __future__ import annotations

import pytest

from chalkcompute._progress import DeployProgress


def test_progress_stays_live_until_all_concurrent_resources_finish(monkeypatch) -> None:
    progress = DeployProgress()
    stops: list[None] = []

    def start() -> None:
        progress._started = True

    monkeypatch.setattr(progress, "_start", start)
    monkeypatch.setattr(progress, "_refresh", lambda: None)
    monkeypatch.setattr(progress, "_stop", lambda: stops.append(None))

    progress.add("first", "function", "First")
    progress.add("second", "function", "Second")
    progress.update("first", status="done")
    progress.finish_if_idle()

    assert stops == []

    progress.update("second", status="done")
    progress.finish_if_idle()

    assert stops == [None]


def test_completed_progress_tracker_is_reused_without_old_rows(monkeypatch) -> None:
    progress = DeployProgress()

    def start() -> None:
        progress._started = True

    def stop() -> None:
        progress._started = False

    monkeypatch.setattr(progress, "_start", start)
    monkeypatch.setattr(progress, "_refresh", lambda: None)
    monkeypatch.setattr(progress, "_stop", stop)

    progress.add("first", "function", "First")
    progress.update("first", status="done")
    progress.finish_if_idle()
    progress.add("second", "function", "Second")

    assert progress._order == ["second"]
    assert set(progress._resources) == {"second"}


def test_failed_root_can_finish_with_an_active_child(monkeypatch) -> None:
    progress = DeployProgress()
    stops: list[None] = []

    monkeypatch.setattr(progress, "_start", lambda: None)
    monkeypatch.setattr(progress, "_refresh", lambda: None)
    monkeypatch.setattr(progress, "_stop", lambda: stops.append(None))

    progress.add("function", "function", "Function")
    progress.add("image", "image", "Image", parent="function")
    progress.update("function", status="failed")
    progress.finish_if_idle()

    assert stops == [None]


def test_reuse_clear_keeps_rows_of_a_still_running_operation(monkeypatch) -> None:
    """A finished operation must not erase a concurrent operation's subtree."""
    progress = DeployProgress()

    def start() -> None:
        progress._started = True

    def stop() -> None:
        progress._started = False

    monkeypatch.setattr(progress, "_start", start)
    monkeypatch.setattr(progress, "_refresh", lambda: None)
    monkeypatch.setattr(progress, "_stop", stop)

    # Operation A registers a root plus a child, then stalls mid-flight.
    progress.add("fn:a", "function", "A")
    progress.add("build:a", "image", "Building A", parent="fn:a")

    # Operation B runs to completion and stops the display.
    progress.add("fn:b", "function", "B")
    progress.update("fn:b", status="done")
    progress._started = False

    # A third operation reuses the tracker; B's rows go, A's subtree stays.
    progress.add("fn:c", "function", "C")

    assert set(progress._resources) == {"fn:a", "build:a", "fn:c"}
    assert progress._order == ["fn:a", "fn:c"]

    # A can still finish rendering its own rows.
    progress.update("build:a", status="done")
    assert progress._resources["build:a"].status == "done"


def test_terminal_root_skips_its_unfinished_children(monkeypatch) -> None:
    """A finished operation has no running steps, so the display can idle."""
    progress = DeployProgress()
    stops: list[None] = []

    def start() -> None:
        progress._started = True

    def stop() -> None:
        progress._started = False
        stops.append(None)

    monkeypatch.setattr(progress, "_start", start)
    monkeypatch.setattr(progress, "_refresh", lambda: None)
    monkeypatch.setattr(progress, "_stop", stop)

    progress.add("fn:a", "function", "A")
    progress.add("build:a", "image", "Building A", parent="fn:a")
    progress.update("fn:a", status="failed")

    # The child cannot still be running once its operation failed.
    assert progress._resources["build:a"].status == "skipped"

    # Both terminality checks now agree, so the display idles...
    progress.finish_if_idle()
    assert stops == [None]

    # ...and the whole subtree is dropped when the tracker is reused.
    progress.add("fn:b", "function", "B")
    assert set(progress._resources) == {"fn:b"}


@pytest.mark.parametrize(
    ("mode", "tty", "ci", "term", "notebook", "expected"),
    [
        (None, True, False, "xterm", False, True),
        ("auto", True, False, "xterm", False, True),
        (None, False, False, "xterm", False, True),
        (None, True, True, "xterm", False, True),
        (None, True, False, "dumb", False, True),
        (None, True, False, "xterm", True, True),
        ("false", True, False, "xterm", False, False),
        ("0", True, False, "xterm", False, False),
        ("true", False, False, "xterm", True, True),
        ("1", True, True, "xterm", False, True),
    ],
)
def test_progress_environment_selection(
    monkeypatch, mode, tty, ci, term, notebook, expected
):
    import sys
    from types import SimpleNamespace
    from chalkcompute._progress import tui_enabled
    import logging

    monkeypatch.setattr(logging.getLogger("chalkcompute"), "hasHandlers", lambda: False)

    if mode is None:
        monkeypatch.delenv("CHALK_SANDBOX_TUI", raising=False)
    else:
        monkeypatch.setenv("CHALK_SANDBOX_TUI", mode)
    monkeypatch.setenv("TERM", term)
    if ci:
        monkeypatch.setenv("CI", "true")
    else:
        monkeypatch.delenv("CI", raising=False)
    if notebook:
        monkeypatch.setitem(sys.modules, "ipykernel", SimpleNamespace())
    else:
        monkeypatch.delitem(sys.modules, "ipykernel", raising=False)
    monkeypatch.setattr(sys, "stderr", SimpleNamespace(isatty=lambda: tty))
    assert tui_enabled() is expected


def test_progress_preserves_logging_and_standard_streams(monkeypatch, caplog):
    import io
    import logging
    import sys

    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    caplog.set_level(logging.DEBUG, logger="chalkcompute")
    logger = logging.getLogger("chalkcompute")
    original_level = logger.level
    original_streams = (sys.stdout, sys.stderr)
    progress = DeployProgress()
    try:
        progress.add("fn", "function", "[literal] fraud")
        assert sys.stdout is original_streams[0]
        assert logger.level == original_level
        logger.debug("debug remains available")
        logger.warning("warning remains visible")
        logger.error("error remains visible")
        # User reconfiguration during a deployment must not be undone on finish.
        logger.setLevel(logging.WARNING)
        progress.update("fn", status="done")
    finally:
        progress.finish()
    assert (sys.stdout, sys.stderr) == original_streams
    assert logger.level == logging.WARNING
    assert [record.getMessage() for record in caplog.records] == [
        "debug remains available",
        "warning remains visible",
        "error remains visible",
    ]
    assert "[literal] fraud" in stderr.getvalue()


def test_forced_progress_uses_stderr_without_ansi_when_redirected(monkeypatch, capsys):
    monkeypatch.setenv("CHALK_SANDBOX_TUI", "true")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("TTY_COMPATIBLE", raising=False)
    progress = DeployProgress()
    try:
        progress.add("fn", "function", "Fraud")
        progress.update("fn", status="done")
    finally:
        progress.finish()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Fraud" in captured.err
    assert "\x1b" not in captured.err


def test_application_logging_does_not_disable_automatic_progress(monkeypatch):
    import logging
    import sys
    from types import SimpleNamespace
    from chalkcompute._progress import tui_enabled

    monkeypatch.delenv("CHALK_SANDBOX_TUI", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delitem(sys.modules, "ipykernel", raising=False)
    monkeypatch.setattr(sys, "stderr", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(logging.getLogger("chalkcompute"), "hasHandlers", lambda: True)
    assert tui_enabled()
    monkeypatch.setenv("CHALK_SANDBOX_TUI", "false")
    assert not tui_enabled()


def test_disabling_progress_stops_existing_display(monkeypatch):
    from chalkcompute import _progress

    progress = DeployProgress()
    stops = []
    monkeypatch.setattr(_progress, "_global_progress", progress)
    monkeypatch.setattr(progress, "finish", lambda: stops.append(True))
    monkeypatch.setenv("CHALK_SANDBOX_TUI", "false")
    assert _progress.get_progress() is None
    assert stops == [True]
    assert _progress._global_progress is None


@pytest.mark.parametrize("fails", [False, True])
def test_scaling_group_progress_finishes_on_success_or_failure(monkeypatch, fails):
    from chalkcompute import _progress
    from chalkcompute._scaling_group import ScalingGroup

    progress = DeployProgress()
    stopped = []
    monkeypatch.setattr(_progress, "get_progress", lambda: progress)
    monkeypatch.setattr(progress, "_start", lambda: None)
    monkeypatch.setattr(progress, "_stop", lambda: stopped.append(True))
    group = ScalingGroup(image="ubuntu:latest", name="fraud")

    def deploy(**kwargs):
        if fails:
            raise RuntimeError("deployment failed")
        return group

    monkeypatch.setattr(group, "_deploy", deploy)
    if fails:
        with pytest.raises(RuntimeError, match="deployment failed"):
            group.deploy()
    else:
        assert group.deploy() is group
    assert progress._resources[f"scaling_group:{id(group)}"].status == (
        "failed" if fails else "done"
    )
    assert stopped == [True]


@pytest.mark.parametrize("no_color", [False, True])
def test_progress_renders_only_changes_on_custom_streams(
    monkeypatch, no_color, no_ci_environment
):
    import io
    import sys

    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delitem(sys.modules, "ipykernel", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
    progress = DeployProgress()
    progress.add("fn", "function", "Fraud")
    progress.add("image", "image", "Build image", parent="fn")
    for _ in range(10):
        progress.update("image", detail="Building")
    progress.update("image", status="done")
    progress.update("fn", status="done", detail="ready")
    progress.finish()
    output = stream.getvalue()
    assert len(output.splitlines()) == 4
    if no_color:
        assert "\033" not in output
        assert output.startswith("› Fraud: Build image\n")
        assert output.endswith("✓ Fraud: Ready\n")
    else:
        assert "\033[36m›\033[0m" in output
        assert "\033[32m✓\033[0m" in output
        assert "\033[32m✓\033[0m \033[1mFraud\033[0m: Ready" in output
    assert "\r" not in output
    assert "\033[2K" not in output


def test_progress_falls_back_for_ascii_streams(monkeypatch):
    import io
    import sys

    data = io.BytesIO()
    stream = io.TextIOWrapper(data, encoding="ascii")
    monkeypatch.setattr(sys, "stderr", stream)
    progress = DeployProgress()
    progress.add("fn", "function", "Fraud")
    progress.update("fn", status="done", detail="ready")
    progress.finish()
    assert data.getvalue() == b"+ Fraud: Ready\n"


def test_deployment_steps_identify_the_resource_on_every_line(capsys):
    progress = DeployProgress()
    progress.add("fn", "function", 'Function "fraud"')
    progress.add("register", "function", "Registering function", parent="fn")
    progress.update("register", status="done", label="Function registered")
    progress.update("fn", detail="Waiting for readiness")
    for _ in range(10):
        progress.update("fn", detail="Pending ready=0 available=0")
    progress.update("fn", detail="Available ready=1 available=1")
    progress.update("fn", status="done", detail="ready")
    progress.finish()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        '› Function "fraud": Registering function',
        '✓ Function "fraud": Function registered',
        '› Function "fraud": Waiting for readiness',
        '› Function "fraud": Pending ready=0 available=0',
        '› Function "fraud": Available ready=1 available=1',
        '✓ Function "fraud": Ready',
    ]


def test_interleaved_groups_restate_identity_and_close_after_their_children(capsys):
    progress = DeployProgress()
    progress.add("a", "function", 'Function "A"')
    progress.add("image", "image", "Build image", parent="a")
    progress.add("b", "function", 'Function "B"')
    progress.update("a", detail="Pending")
    progress.update("b", status="done", detail="ready")
    progress.update("a", status="failed", detail="")
    progress.finish()
    assert capsys.readouterr().err.splitlines() == [
        '› Function "A": Build image',
        '› Function "A": Pending',
        '✓ Function "B": Ready',
        '– Function "A": Build image',
        '✗ Function "A": Failed',
    ]


def test_declarations_alone_are_silent(capsys):
    progress = DeployProgress()
    progress.add("a", "function", 'Function "A"')
    progress.add("b", "function", 'Function "B"')
    assert capsys.readouterr().err == ""
    progress.add("register", "function", "Registering function", parent="a")
    assert capsys.readouterr().err == '› Function "A": Registering function\n'
    progress.finish()


def test_nested_steps_include_parent_identity(capsys):
    progress = DeployProgress()
    progress.add("cls", "class", 'Class "Fraud"')
    progress.add("method", "method", ".score()", parent="cls")
    progress.add("image", "image", "Building image", parent="method")
    assert capsys.readouterr().err.splitlines() == [
        '› Class "Fraud": .score()',
        '› Class "Fraud" / .score(): Building image',
    ]
    progress.finish()


@pytest.mark.parametrize("failure", [BrokenPipeError, ValueError])
def test_closed_or_broken_stream_does_not_fail_deployment(monkeypatch, failure):
    import io
    import sys

    class BrokenStream(io.StringIO):
        def write(self, value):
            raise failure("closed")

    monkeypatch.setattr(sys, "stderr", BrokenStream())
    progress = DeployProgress()
    progress.add("fn", "function", "Fraud", detail="Registering")
    progress.update("fn", status="done")
    progress.finish()


def test_status_text_cannot_inject_terminal_controls(capsys):
    progress = DeployProgress()
    progress.add("fn", "function", "[bold]Fraud\x1b[2J", detail="Pending\r\nNext\x07")
    progress.finish()
    assert capsys.readouterr().err == "› [bold]Fraud: Pending Next\n"
