"""Shared pytest fixtures for unit tests.

Tests should not depend on the developer's (or CI's) ambient Chalk
credentials. The autouse fixture below clears every CHALK_* env var by
default; tests that need them set use ``monkeypatch.setenv(...)``
explicitly.
"""

from __future__ import annotations

from typing import Iterator

import pytest

_CHALK_ENV_VARS = (
    "CHALK_CLIENT_ID",
    "CHALK_CLIENT_SECRET",
    "CHALK_API_SERVER",
    "CHALK_ENVIRONMENT",
    "CHALK_ENVIRONMENT_ID",
    "CHALK_ACTIVE_ENVIRONMENT",
    "_CHALK_CLIENT_ID",
    "_CHALK_CLIENT_SECRET",
    "_CHALK_ACTIVE_ENVIRONMENT",
    "_CHALK_API_SERVER",
)


@pytest.fixture(autouse=True)
def _isolate_chalk_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in _CHALK_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_chalk_yml(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """Point ~/.chalk.yml lookup at an empty dir so tests don't see real tokens.

    `_load_global_config_uncached` reads ``$XDG_CONFIG_HOME/.chalk.yml`` or
    ``$HOME/.chalk.yml``. Pointing both at an empty tmpdir makes the lookup
    return ``None`` without patching any internals — tests that want a
    specific config use ``monkeypatch.setenv("XDG_CONFIG_HOME", ...)``.
    """
    import chalkcompute._auth_config as _auth_config

    empty = tmp_path_factory.mktemp("chalk-empty-home")
    monkeypatch.setenv("HOME", str(empty))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty))
    _auth_config._load_global_config.cache_clear()
    yield
