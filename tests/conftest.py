"""Fixtures shared by the scorer tests."""

from __future__ import annotations

import pytest

from chalkcompute import scorers
from chalkcompute._function import RemoteFunction


@pytest.fixture
def no_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never start a deploy: a background thread outliving the test would reach
    the real one after monkeypatch had restored it."""
    monkeypatch.setattr(RemoteFunction, "_start_background_deploy", lambda self: None)
    monkeypatch.setattr(scorers, "should_strip_source", lambda: False)
