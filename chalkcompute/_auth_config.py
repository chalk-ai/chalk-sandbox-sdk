from __future__ import annotations

import itertools
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

logger = logging.getLogger(__name__)


# Where a setting came from, described with the wording `chalk config` uses.


@dataclass(frozen=True)
class ArgumentSource:
    name: str

    @property
    def description(self) -> str:
        return f"{self.name} argument"


@dataclass(frozen=True)
class EnvVarSource:
    name: str

    @property
    def description(self) -> str:
        return f"env var {self.name}"


@dataclass(frozen=True)
class TokenSource:
    """A ~/.chalk.yml token, keyed by project directory or ``default``."""

    path: str

    @property
    def description(self) -> str:
        return f"{self.path} token"

    @property
    def is_default(self) -> bool:
        return self.path == "default"


@dataclass(frozen=True)
class PrimaryEnvironmentSource:
    """No environment was requested, so the credentials' primary one is used."""

    @property
    def description(self) -> str:
        return "primary environment of the credentials"


Source = ArgumentSource | EnvVarSource | TokenSource | PrimaryEnvironmentSource


@dataclass
class TokenConfig:
    clientId: str
    clientSecret: str
    name: str | None = None
    apiServer: str | None = None
    activeEnvironment: str | None = None
    environmentSource: Source | None = None

    @staticmethod
    def from_py(raw: Any, path: str) -> TokenConfig:
        raw = _dict_with_str_keys(raw, name=path)
        prefix = f"tokens.{path}"
        return TokenConfig(
            clientId=_string(raw.get("clientId"), f"{prefix}.clientId"),
            clientSecret=_string(raw.get("clientSecret"), f"{prefix}.clientSecret"),
            name=_optional_string(raw.get("name"), f"{prefix}.name"),
            apiServer=_optional_string(raw.get("apiServer"), f"{prefix}.apiServer"),
            activeEnvironment=_optional_string(
                raw.get("activeEnvironment"),
                f"{prefix}.activeEnvironment",
            ),
        )


@dataclass
class AuthConfig:
    tokens: Mapping[str, TokenConfig] | None

    @staticmethod
    def from_py(raw: Any) -> AuthConfig:
        value = _dict_with_str_keys_or_none(raw, name="auth config")
        if value is None:
            return AuthConfig(tokens=None)
        tokens = _dict_with_str_keys_or_none(value.get("tokens", None), "tokens")
        if tokens is None:
            return AuthConfig(tokens=None)
        return AuthConfig(
            tokens={k: TokenConfig.from_py(v, path=k) for k, v in tokens.items()},
        )


@dataclass
class ProjectSettings:
    local_path: str


def _string(value: Any, name: str) -> str:
    if value is None:
        raise ValueError(f"Expected a string, but found nothing for '{name}'")
    if not isinstance(value, str):
        raise ValueError(f"Expected a string, but found '{value}' for '{name}'")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    return None if value is None else _string(value=value, name=name)


def _dict_with_str_keys(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Expected dict for '{name}', got '{value}'")
    for key in value.keys():
        if not isinstance(key, str):
            raise ValueError(f"Expected string key for '{name}', got '{key}'")
    return value


def _dict_with_str_keys_or_none(value: Any, name: str) -> Mapping[str, Any] | None:
    return None if value is None else _dict_with_str_keys(value, name)


def _load_global_config_uncached() -> AuthConfig | None:
    path = None
    p = os.path.join(
        os.getenv("XDG_CONFIG_HOME", os.path.expanduser("~")),
        ".chalk.yml",
    )
    if os.path.exists(p):
        path = p

    if path is None:
        return None

    with open(path) as f:
        parsed = yaml.safe_load(f)
    return AuthConfig.from_py(parsed)


@lru_cache(maxsize=None)
def _load_global_config() -> AuthConfig | None:
    return _load_global_config_uncached()


def _en(current_env_name: str, old_env_name: str | None = None) -> str | None:
    old_env_name = old_env_name or f"_{current_env_name}"
    deprecated = os.getenv(old_env_name)
    current = os.getenv(current_env_name)
    if deprecated and current:
        logger.warning(
            "Using both deprecated '%s' and new '%s' environment variables. "
            "Choosing '%s'",
            old_env_name,
            current_env_name,
            current_env_name,
        )
    return current or deprecated


def active_environment_from_env() -> str | None:
    found = active_environment_variable_from_env()
    return None if found is None else found[1]


def active_environment_variable_from_env() -> tuple[str, str] | None:
    """Return the name and value of the environment variable selecting the env."""
    value = _en("CHALK_ENVIRONMENT", "_CHALK_ACTIVE_ENVIRONMENT")
    if value:
        name = (
            "CHALK_ENVIRONMENT"
            if os.getenv("CHALK_ENVIRONMENT")
            else "_CHALK_ACTIVE_ENVIRONMENT"
        )
        return name, value
    for name in ("CHALK_ENVIRONMENT_ID", "CHALK_ACTIVE_ENVIRONMENT"):
        value = os.getenv(name)
        if value:
            return name, value
    return None


def load_token(
    client_id: str | None,
    client_secret: str | None,
    active_environment: str | None,
    api_server: str | None,
    skip_cache: bool = False,
) -> TokenConfig | None:
    token_name = "Explicit" if client_id and client_secret else "Environment"

    client_id = client_id or _en("CHALK_CLIENT_ID")
    client_secret = client_secret or _en("CHALK_CLIENT_SECRET")
    api_server = api_server or _en("CHALK_API_SERVER")
    environment_source: Source | None = None
    if active_environment:
        environment_source = ArgumentSource("active_environment")
    else:
        found = active_environment_variable_from_env()
        if found is not None:
            name, active_environment = found
            environment_source = EnvVarSource(name)

    if client_id is not None and client_secret is not None:
        return TokenConfig(
            name=token_name,
            clientId=client_id,
            clientSecret=client_secret,
            apiServer=api_server or "https://api.chalk.ai",
            activeEnvironment=active_environment,
            environmentSource=environment_source or PrimaryEnvironmentSource(),
        )

    global_cfg = _load_global_config_uncached() if skip_cache else _load_global_config()
    if global_cfg is None:
        return None

    absdir = Path(os.getcwd()).absolute().resolve()
    project_config = load_project_config()
    if project_config:
        absdir = Path(project_config.local_path).parent
    tokens: Mapping[str, TokenConfig] = global_cfg.tokens or {}
    token_source = TokenSource(str(absdir))
    token_config = tokens.get(token_source.path)
    if token_config is None:
        token_source = TokenSource("default")
        token_config = tokens.get(token_source.path)
    if token_config is None:
        return None

    return TokenConfig(
        name=token_config.name,
        clientId=client_id or token_config.clientId,
        clientSecret=client_secret or token_config.clientSecret,
        apiServer=api_server or token_config.apiServer or "https://api.chalk.ai",
        activeEnvironment=active_environment or token_config.activeEnvironment,
        environmentSource=environment_source or token_source,
    )


def _load_project_config_at_path(filename: Path) -> ProjectSettings | None:
    try:
        with open(filename) as f:
            yaml.safe_load(f)
        return ProjectSettings(local_path=str(filename.absolute().resolve()))
    except OSError:
        return None
    except ValueError as exc:
        raise ValueError(
            f"Failed to load project config from {filename}: {exc}"
        ) from exc


def load_project_config() -> ProjectSettings | None:
    base = Path(os.getcwd())

    for directory in itertools.chain([base], base.parents):
        project = _load_project_config_at_path(
            directory / "chalk.yaml",
        ) or _load_project_config_at_path(directory / "chalk.yml")

        if project is not None:
            return project

    return None
