from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import textwrap

import pytest

import chalkcompute
from chalkcompute import _logging
from chalkcompute._logging import _StatusLogger


def _run(code: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("CHALK_SANDBOX_TUI", None)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )


@pytest.mark.parametrize("configure_before_import", [False, True])
def test_import_leaves_application_logging_untouched(configure_before_import) -> None:
    result = _run(
        f"""
        import logging
        import sys
        root = logging.getLogger()
        sdk = logging.getLogger("chalkcompute")
        if {configure_before_import!r}:
            logging.basicConfig(level=logging.INFO, format="APP %(message)s")
        before = [(log.level, log.handlers[:], log.propagate) for log in (root, sdk)]
        import chalkcompute
        import chalkcompute._function
        assert before == [(log.level, log.handlers[:], log.propagate) for log in (root, sdk)]
        assert "rich.logging" not in sys.modules
        logging.getLogger("chalkcompute._function").info("registered")
        logging.getLogger("chalkcompute._function").warning("warning [literal]")
        print('{{"result": 1}}')
        """
    )
    assert result.stdout == '{"result": 1}\n'
    if configure_before_import:
        assert result.stderr == "APP registered\nAPP warning [literal]\n"
    else:
        # Python's lastResort handler keeps warnings visible with no configuration.
        assert result.stderr == "warning [literal]\n"


def test_application_can_configure_logging_after_sdk_import() -> None:
    result = _run(
        """
        import chalkcompute
        import logging
        logging.basicConfig(level=logging.INFO, format="APP %(message)s")
        logging.getLogger("chalkcompute._function").info("registered")
        """
    )
    assert result.stdout == ""
    assert result.stderr == "APP registered\n"


@pytest.fixture
def isolated_sdk_logger(monkeypatch):
    logger = logging.getLogger("chalkcompute")
    handlers, level, propagate, disabled = (
        logger.handlers[:],
        logger.level,
        logger.propagate,
        logger.disabled,
    )
    logger.handlers = []
    logger.disabled = False
    monkeypatch.setattr(_logging, "_console_handler", None)
    try:
        yield logger
    finally:
        for handler in logger.handlers:
            if handler not in handlers:
                handler.close()
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate
        logger.disabled = disabled


def test_console_logging_is_literal_unwrapped_and_only_on_stderr(
    isolated_sdk_logger, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("COLUMNS", "20")
    root = logging.getLogger()
    root_state = (root.level, root.handlers[:])
    chalkcompute.configure_logging()
    logger = logging.getLogger("chalkcompute._function")
    message = "[bold]ready[/bold] https://example.com/" + "x" * 200
    logger.info(message)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"INFO chalkcompute._function: {message}\n"
    assert (root.level, root.handlers) == root_state


def test_console_configuration_is_repeatable_and_preserves_custom_handlers(
    isolated_sdk_logger,
) -> None:
    custom_output, first, second = io.StringIO(), io.StringIO(), io.StringIO()
    custom = logging.StreamHandler(custom_output)
    isolated_sdk_logger.addHandler(custom)
    chalkcompute.configure_logging("INFO", stream=first)
    isolated_sdk_logger.info("first")
    chalkcompute.configure_logging("DEBUG", stream=second)
    isolated_sdk_logger.debug("second")
    assert first.getvalue() == "INFO chalkcompute: first\n"
    assert second.getvalue() == "DEBUG chalkcompute: second\n"
    assert custom_output.getvalue() == "first\nsecond\n"
    assert len(isolated_sdk_logger.handlers) == 2


def test_invalid_configuration_preserves_working_handler(isolated_sdk_logger) -> None:
    output = io.StringIO()
    chalkcompute.configure_logging(stream=output)
    with pytest.raises(ValueError):
        chalkcompute.configure_logging("not-a-level")
    isolated_sdk_logger.info("still works")
    assert output.getvalue() == "INFO chalkcompute: still works\n"


def test_console_helper_does_not_duplicate_records_at_root() -> None:
    result = _run(
        """
        import logging
        import chalkcompute
        logging.basicConfig(level=logging.INFO, format="ROOT %(message)s")
        chalkcompute.configure_logging()
        chalkcompute.configure_logging()
        logging.getLogger("chalkcompute._function").info("registered")
        logging.getLogger("application").info("app still works")
        """
    )
    assert result.stderr == (
        "INFO chalkcompute._function: registered\nROOT app still works\n"
    )


def test_status_logs_changes_once_and_retains_every_poll_at_debug(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="chalkcompute")
    status = _StatusLogger(logging.getLogger("chalkcompute.test"), "Function fraud")
    for _ in range(10):
        status.update("Pending", ready=0, available=0)
    status.update("Pending", ready=0, available=0, message="waiting for startup probe")
    status.update("Available", ready=1, available=1)
    assert len(caplog.records) == 12
    info = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    ]
    assert info == [
        "Function fraud: Pending ready=0 available=0",
        "Function fraud: Pending ready=0 available=0 — waiting for startup probe",
        "Function fraud: Available ready=1 available=1",
    ]
    assert sum(record.levelno == logging.DEBUG for record in caplog.records) == 9


def test_status_deduplication_is_per_operation(caplog) -> None:
    caplog.set_level(logging.INFO, logger="chalkcompute")
    logger = logging.getLogger("chalkcompute.test")
    for _ in range(2):
        status = _StatusLogger(logger, "Function fraud")
        status.update("Pending")
        status.update("Pending")
    assert len(caplog.records) == 2


@pytest.mark.parametrize(
    ("tty", "color", "no_color", "term", "ci", "notebook", "expected_color"),
    [
        (True, True, False, "xterm", False, False, True),
        (False, True, False, "xterm", False, False, False),
        (True, False, False, "xterm", False, False, False),
        (True, True, True, "xterm", False, False, False),
        (True, True, False, "dumb", False, False, False),
        (True, True, False, "xterm", True, False, False),
        (True, True, False, "xterm", False, True, False),
    ],
)
def test_console_color_policy(
    isolated_sdk_logger,
    monkeypatch,
    tty,
    color,
    no_color,
    term,
    ci,
    notebook,
    expected_color,
):
    from types import SimpleNamespace

    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: tty)
    monkeypatch.setenv("TERM", term)
    for name, enabled in [("NO_COLOR", no_color), ("CI", ci)]:
        if enabled:
            monkeypatch.setenv(name, "1")
        else:
            monkeypatch.delenv(name, raising=False)
    if notebook:
        monkeypatch.setitem(sys.modules, "ipykernel", SimpleNamespace())
    else:
        monkeypatch.delitem(sys.modules, "ipykernel", raising=False)

    chalkcompute.configure_logging(stream=stream, color=color)
    # A second application handler must see an unmodified record, even when it
    # runs after the console formatter has applied its colors.
    plain = io.StringIO()
    handler = logging.StreamHandler(plain)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    isolated_sdk_logger.addHandler(handler)
    isolated_sdk_logger.warning("[literal] warning")
    prefix = "\033[33mWARNING\033[0m" if expected_color else "WARNING"
    assert stream.getvalue() == f"{prefix} chalkcompute: [literal] warning\n"
    assert plain.getvalue() == "WARNING: [literal] warning\n"


def test_logging_and_progress_work_without_site_packages():
    # -S makes third-party packages (including Rich) unavailable entirely.
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            textwrap.dedent("""
            import logging
            import sys
            import chalkcompute
            from chalkcompute._progress import DeployProgress
            progress = DeployProgress()
            progress.add("fn", "function", 'Function "fraud"')
            for _ in range(10):
                progress.update("fn", detail="Pending")
            logging.getLogger("chalkcompute").warning("warning stays visible")
            progress.update("fn", status="done", detail="ready")
            progress.finish()
            chalkcompute.configure_logging()
            logging.getLogger("chalkcompute").info("finished")
            assert "rich" not in sys.modules
            print('{"result": 42}')
        """),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert result.stdout == '{"result": 42}\n'
    assert result.stderr.splitlines() == [
        '› Function "fraud"',
        '› Function "fraud" — Pending',
        "warning stays visible",
        '✓ Function "fraud" — ready',
        "INFO chalkcompute: finished",
    ]


def test_sdk_runtime_dependencies_do_not_include_rich():
    import tomllib
    from pathlib import Path
    from chalkcompute._local_dev import _LOCAL_SDK_RUNTIME_PACKAGES

    metadata = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert not any(
        dep.startswith("rich") for dep in metadata["project"]["dependencies"]
    )
    assert not any(dep.startswith("rich") for dep in _LOCAL_SDK_RUNTIME_PACKAGES)
