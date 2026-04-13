"""Secret references for injecting secrets into Containers as environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

_SECRETS_SVC = "chalk.server.v1.EnvironmentSecretsService"


@dataclass(frozen=True)
class Secret:
    """A reference to a Chalk secret or integration to inject as env vars.

    Use the factory methods to create instances:

        # Reference an existing Chalk secret
        Secret.from_env("OPENAI_API_TOKEN")

        # Reference an existing Chalk integration
        Secret.from_integration("prod_postgres")

        # Inject a local env var into the remote environment
        Secret.from_local_env("OPENAI_API_KEY")
        Secret.from_local_env("OPENAI_API_KEY", env_var_name="API_KEY")

        # Inject all vars from a local .env file
        Secret.from_local_env_file(".env")
    """

    _integration_name: str | None = field(default=None, repr=False)
    _secret_name: str | None = field(default=None, repr=False)
    _keys: list[str] = field(default_factory=list, repr=False)
    _aliases: dict[str, str] = field(default_factory=dict, repr=False)
    _prefix: str | None = field(default=None, repr=False)

    # -- Lazy local secret fields (resolved at Container.run() time) --
    _local_env_var: str | None = field(default=None, repr=False)
    _local_env_file: str | None = field(default=None, repr=False)
    _env_var_name: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(
        cls,
        name: str,
        *,
        alias: str | None = None,
        prefix: str | None = None,
    ) -> Secret:
        """Reference a standalone secret by name (e.g. ``"OPENAI_API_TOKEN"``).

        The secret's value is injected as an environment variable with the same
        name, unless *alias* or *prefix* is given.
        """
        aliases = {name: alias} if alias else {}
        return cls(
            _secret_name=name,
            _aliases=aliases,
            _prefix=prefix,
        )

    @classmethod
    def from_integration(
        cls,
        name: str,
        *,
        keys: list[str] | None = None,
        aliases: dict[str, str] | None = None,
        prefix: str | None = None,
    ) -> Secret:
        """Reference an integration by name (e.g. ``"prod_postgres"``).

        All secrets associated with the integration are injected as env vars.
        Use *keys* to limit which keys are injected, *aliases* to remap names,
        or *prefix* to add a common prefix.
        """
        return cls(
            _integration_name=name,
            _keys=list(keys or []),
            _aliases=dict(aliases or {}),
            _prefix=prefix,
        )

    @classmethod
    def from_local_env(
        cls,
        local_env_var: str,
        *,
        env_var_name: str | None = None,
    ) -> Secret:
        """Inject a local environment variable as a secret into the container.

        Reads ``os.environ[local_env_var]`` at container start time, upserts it
        as a Chalk secret, and injects it as an env var in the container.

        Args:
            local_env_var: Name of the local environment variable to read.
            env_var_name: Name for the env var inside the container.
                          Defaults to *local_env_var*.
        """
        return cls(
            _local_env_var=local_env_var,
            _env_var_name=env_var_name,
        )

    @classmethod
    def from_local_env_file(
        cls,
        path: str,
    ) -> Secret:
        """Inject all variables from a local ``.env`` file as secrets.

        Reads the file at container start time, upserts each ``KEY=VALUE``
        pair as a Chalk secret, and injects them as env vars in the container.
        Lines starting with ``#`` and blank lines are ignored.
        """
        return cls(
            _local_env_file=path,
        )

    @property
    def is_lazy(self) -> bool:
        """True if this secret requires local resolution before container start."""
        return self._local_env_var is not None or self._local_env_file is not None

    def _to_proto_dict(self) -> dict[str, Any]:
        """Serialize to the dict shape expected by the SecretRef protobuf."""
        d: dict[str, Any] = {}
        if self._integration_name is not None:
            d["integrationName"] = self._integration_name
        elif self._secret_name is not None:
            d["secretName"] = self._secret_name
        if self._keys:
            d["keys"] = self._keys
        if self._aliases:
            d["aliases"] = self._aliases
        if self._prefix is not None:
            d["prefix"] = self._prefix
        return d

    def __repr__(self) -> str:
        if self._local_env_file:
            return f"Secret.from_local_env_file({self._local_env_file!r})"
        if self._local_env_var:
            name = self._env_var_name or self._local_env_var
            return f"Secret.from_local_env({self._local_env_var!r}, env_var_name={name!r})"
        if self._secret_name:
            return f"Secret.from_env({self._secret_name!r})"
        return f"Secret.from_integration({self._integration_name!r})"


def _parse_env_file(path: str) -> dict[str, str]:
    """Parse a simple .env file into a dict of key-value pairs."""
    result: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            # Strip optional surrounding quotes
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            result[key] = value
    return result


def resolve_lazy_secrets(
    secrets: list[Secret],
    client: Any,
) -> tuple[list[Secret], list[str]]:
    """Resolve lazy secrets (from_local_env / from_local_env_file) into concrete secrets.

    Reads local env vars / files, upserts them as Chalk secrets, and returns:
    - A new list of Secret objects (lazy ones replaced with from_env equivalents)
    - A list of upserted secret names (for cleanup on stop)
    """
    resolved: list[Secret] = []
    upserted_names: list[str] = []

    for secret in secrets:
        if secret._local_env_var is not None:
            local_var = secret._local_env_var
            value = os.environ.get(local_var)
            if value is None:
                raise ValueError(
                    f"Local environment variable {local_var!r} is not set"
                )
            remote_name = secret._env_var_name or local_var
            _upsert_secret(client, remote_name, value)
            upserted_names.append(remote_name)
            resolved.append(Secret.from_env(remote_name))

        elif secret._local_env_file is not None:
            env_vars = _parse_env_file(secret._local_env_file)
            for name, value in env_vars.items():
                _upsert_secret(client, name, value)
                upserted_names.append(name)
                resolved.append(Secret.from_env(name))

        else:
            resolved.append(secret)

    return resolved, upserted_names


def _upsert_secret(client: Any, name: str, value: str) -> None:
    """Upsert a secret via EnvironmentSecretsService."""
    resp = client.rpc(
        _SECRETS_SVC,
        "UpsertSecret",
        {"name": name, "config": {"literal": value}},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to upsert secret {name!r}: {resp.text}")


def delete_secrets(client: Any, names: list[str]) -> None:
    """Delete secrets by name via EnvironmentSecretsService."""
    for name in names:
        client.rpc(_SECRETS_SVC, "DeleteSecret", {"name": name})
