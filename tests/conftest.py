"""Shared test fixtures."""

from __future__ import annotations

import pytest

from chalkcompute import scorers
from chalkcompute._function import RemoteFunction


@pytest.fixture
def no_ci_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let terminal-policy tests choose CI state instead of inheriting the runner's.

    Explicitly requested rather than autouse so other tests still exercise the
    actual host environment, including subprocesses and deployment behavior.
    """
    for name in ("CI", "BUILDKITE", "GITHUB_ACTIONS", "TF_BUILD"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never start a deploy: a background thread outliving the test would reach
    the real one after monkeypatch had restored it."""
    monkeypatch.setattr(RemoteFunction, "_start_background_deploy", lambda self: None)
    monkeypatch.setattr(scorers, "should_strip_source", lambda: False)
