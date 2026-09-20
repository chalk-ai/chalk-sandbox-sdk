from __future__ import annotations

import linecache
import re
import traceback

import pytest

from chalkcompute._traceback import format_exception, format_frame


def test_frame_shows_numbered_context_and_preserves_indentation(tmp_path):
    source = tmp_path / "example.py"
    source.write_text(
        "# outside context\n# before\n\n@function\ndef testing():\n"
        "    return 42\n\n# after\n# outside context\n"
    )
    result = format_frame(traceback.FrameSummary(str(source), 5, "testing"))
    assert result == (
        f'  File "{source}", line 5, in testing\n'
        "    2 | # before\n"
        "    3 | \n"
        "    4 | @function\n"
        "  > 5 | def testing():\n"
        "    6 |     return 42\n"
        "    7 | \n"
        "    8 | # after\n\n"
    )


@pytest.mark.parametrize("lineno", [1, 2])
def test_context_stops_at_file_boundaries(tmp_path, lineno):
    source = tmp_path / "short.py"
    source.write_text("first\nlast\n")
    result = format_frame(traceback.FrameSummary(str(source), lineno, "short"))
    assert f"> {lineno} |" in result
    assert result.count(" | ") == 2


def test_expression_marker_handles_unicode_and_tabs(tmp_path):
    source = tmp_path / "unicode.py"
    source.write_text('\tresult = "é" / 0\n', encoding="utf-8")
    result = format_frame(
        traceback.FrameSummary(
            str(source), 1, "testing", end_lineno=1, colno=10, end_colno=18
        )
    )
    assert '  > 1 |     result = "é" / 0\n' in result
    assert "      |              ^^^^^^^\n" in result


def test_missing_source_keeps_location_and_cached_line(tmp_path):
    source = tmp_path / "deleted.py"
    source.write_text("raise ValueError('oops')\n")
    frame = traceback.FrameSummary(str(source), 1, "testing")
    source.unlink()
    linecache.clearcache()
    result = format_frame(frame)
    assert f'File "{source}", line 1, in testing' in result
    assert "> 1 | raise ValueError('oops')" in result
    assert "line 12, in unknown" in format_frame(
        traceback.FrameSummary("<unavailable>", 12, "unknown")
    )


def test_notebook_source_uses_linecache(monkeypatch):
    filename = "<ipython-input-42>"
    monkeypatch.setitem(
        linecache.cache,
        filename,
        (0, None, ["@function\n", "def testing():\n"], filename),
    )
    result = format_frame(traceback.FrameSummary(filename, 1, "testing"))
    assert "> 1 | @function" in result
    assert "2 | def testing():" in result


@pytest.mark.parametrize("compact", [False, True])
def test_chained_exceptions_preserve_notes_and_multiline_messages(compact):
    try:
        try:
            raise ValueError("original problem")
        except ValueError as cause:
            error = RuntimeError("deployment failed\n  run this command\n")
            error.add_note("diagnostic note")
            raise error from cause
    except RuntimeError as error:
        result = format_exception(error, error.__traceback__, compact=compact)
    assert "ValueError: original problem" in result
    assert "The above exception was the direct cause" in result
    assert "RuntimeError: deployment failed\n  run this command\n" in result
    assert "diagnostic note" in result
    assert ("Traceback (most recent call last)" in result) != compact
    assert (" | " in result) != compact


def test_suppressed_context_stays_suppressed():
    try:
        try:
            raise ValueError("hidden message")
        except ValueError:
            raise RuntimeError("visible message") from None
    except RuntimeError as error:
        result = format_exception(error, error.__traceback__, compact=True)
    assert result == "RuntimeError: visible message\n"


def test_exception_groups_keep_nested_stacks_and_notes():
    try:
        raise ValueError("child failure")
    except ValueError as child:
        child.add_note("child note")
        group = ExceptionGroup(
            "deployment failures", [ExceptionGroup("nested", [child])]
        )
    result = format_exception(group, None)
    assert "ExceptionGroup: deployment failures" in result
    assert "ExceptionGroup: nested" in result
    assert "ValueError: child failure\n" in result
    assert "child note" in result
    assert "> " in result
    assert '|         raise ValueError("child failure")' in result


def test_syntax_errors_retain_source_and_caret():
    try:
        compile("def broken(:\n", "example.py", "exec")
    except SyntaxError as error:
        result = format_exception(error, error.__traceback__)
    assert "def broken(:" in result
    assert "^" in result
    assert "SyntaxError: invalid syntax" in result


def test_recursive_stacks_are_folded_and_do_not_capture_locals():
    class Secret:
        def __repr__(self):
            raise AssertionError("local values must never be inspected")

    def recurse():
        secret = Secret()  # noqa: F841
        return recurse()

    try:
        recurse()
    except RecursionError as error:
        result = format_exception(error, error.__traceback__)
    assert "Previous line repeated" in result
    assert len(result) < 10000


def test_colored_frame_preserves_text_and_highlights_location_and_expression(tmp_path):
    source = tmp_path / "example.py"
    source.write_text("value = 1 / 0\n")
    frame = traceback.FrameSummary(
        str(source), 1, "testing", end_lineno=1, colno=8, end_colno=13
    )
    colored = format_frame(frame, color=True)
    assert re.sub(r"\x1b\[[0-9;]*m", "", colored) == format_frame(frame)
    assert f"\033[36m{source}\033[0m" in colored
    assert "\033[35mtesting\033[0m" in colored
    assert "\033[1;31m  > 1 |\033[0m" in colored
    assert "\033[1;33mvalue = 1 / 0\033[0m" in colored
    assert "^^^^^\033[0m" in colored


@pytest.mark.parametrize("compact", [False, True])
def test_color_preserves_chains_groups_notes_and_messages(compact):
    try:
        try:
            raise ValueError("original problem")
        except ValueError as cause:
            error = RuntimeError("failed\n  run this command\nhttps://example.com")
            error.add_note("diagnostic note")
            raise error from cause
    except RuntimeError as error:
        group = ExceptionGroup("failures", [error])
    colored = format_exception(group, None, compact=compact, color=True)
    plain = format_exception(group, None, compact=compact)
    assert re.sub(r"\x1b\[[0-9;]*m", "", colored) == plain
    for name in ["ExceptionGroup", "RuntimeError", "ValueError"]:
        assert f"\033[1;31m{name}\033[0m:" in colored
    assert "  run this command\n" in colored
    assert "https://example.com\n" in colored
    assert "diagnostic note\n" in colored
