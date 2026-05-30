"""Secret references for injecting secrets into Containers as environment variables."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_SECRETS_SVC = "chalk.server.v1.EnvironmentSecretsService"

_AMBIENT_CHALK_ENV_VARS = (
    "CHALK_CLIENT_ID",
    "CHALK_CLIENT_SECRET",
    "CHALK_API_SERVER",
    "CHALK_ENVIRONMENT_ID",
)


@dataclass(frozen=True)
class Secret:
    """A reference to a Chalk secret or integration to inject as env vars.

    Construct instances via the factory methods rather than instantiating
    directly.

    Examples
    --------
    >>> Secret.from_env("OPENAI_API_TOKEN")
    >>> Secret.from_integration("prod_postgres")
    >>> Secret.from_local_env("OPENAI_API_KEY")
    >>> Secret.from_local_env("OPENAI_API_KEY", env_var_name="API_KEY")
    >>> Secret.from_local_env_file(".env")
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

        The secret's value is injected as an environment variable with the
        same name, unless ``alias`` or ``prefix`` is given.

        Parameters
        ----------
        name
            Name of the Chalk secret.
        alias
            Rename the env var exposed in the container.
        prefix
            Optional prefix applied to the env var name.

        Returns
        -------
        Secret
            A secret reference.
        """
        if not name or not name.strip():
            raise ValueError("secret name cannot be empty")
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

        Parameters
        ----------
        name
            Name of the Chalk integration.
        keys
            Limit injection to the listed keys.
        aliases
            Rename specific keys when exposing them as env vars.
        prefix
            Optional prefix applied to every injected env var.

        Returns
        -------
        Secret
            A secret reference bound to the named integration.
        """
        if not name or not name.strip():
            raise ValueError("integration name cannot be empty")
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

        Parameters
        ----------
        local_env_var
            Name of the local environment variable to read.
        env_var_name
            Name for the env var inside the container.
            Defaults to ``local_env_var``.

        Returns
        -------
        Secret
            A lazy secret reference resolved at ``Container.run()`` time.
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

        Parameters
        ----------
        path
            Path to the local ``.env`` file.

        Returns
        -------
        Secret
            A lazy secret reference resolved at ``Container.run()`` time.
        """
        return cls(
            _local_env_file=path,
        )

    @property
    def is_lazy(self) -> bool:
        """Whether this secret requires local resolution before container start.

        Returns
        -------
        bool
            ``True`` if the secret is built from a local env var or env file.
        """
        return self._local_env_var is not None or self._local_env_file is not None

    def _to_proto_dict(self) -> dict[str, Any]:
        """Serialize to the dict shape expected by the ``SecretRef`` protobuf.

        Returns
        -------
        dict[str, Any]
            A JSON-compatible dict representation.
        """
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
        if self._secret_name is not None:
            return f"Secret.from_env({self._secret_name!r})"
        return f"Secret.from_integration({self._integration_name!r})"


def _parse_env_file(path: str) -> dict[str, str]:
    """Parse a simple ``.env`` file into a dict of key-value pairs.

    Parameters
    ----------
    path
        Path to the ``.env`` file.

    Returns
    -------
    dict[str, str]
        Mapping of variable names to values.
    """
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
    """Resolve lazy secrets into concrete secrets.

    Reads local env vars and ``.env`` files referenced by lazy secrets,
    upserts them as Chalk secrets, and returns replacements.

    Parameters
    ----------
    secrets
        The list of secret references, possibly including lazy ones.
    client
        The Connect client used to upsert secrets remotely.

    Returns
    -------
    tuple[list[Secret], list[str]]
        The resolved secrets (lazy ones replaced with ``from_env`` equivalents)
        and the names of secrets that were upserted for cleanup purposes.

    Raises
    ------
    ValueError
        If a referenced local environment variable is not set.
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
    """Upsert a secret via ``EnvironmentSecretsService``.

    Parameters
    ----------
    client
        Connect client used for the RPC.
    name
        Name of the secret to upsert.
    value
        Literal secret value.

    Raises
    ------
    RuntimeError
        If the upsert RPC fails.
    """
    resp = client.rpc(
        _SECRETS_SVC,
        "UpsertSecret",
        {"name": name, "config": {"literal": value}},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to upsert secret {name!r}: {resp.text}")


def _container_env_names_covered(
    existing_secrets: list[Secret],
    existing_env: dict[str, str] | None,
) -> set[str]:
    """Names that the user has already arranged to set inside the container.

    Used by ``resolve_ambient_chalk_secrets`` to avoid duplicating an env var
    the user already specified explicitly (via ``Secret`` or ``env={...}``).
    """
    covered: set[str] = set()
    if existing_env:
        covered.update(existing_env.keys())
    for s in existing_secrets:
        if s._env_var_name is not None:
            covered.add(s._env_var_name)
        elif s._local_env_var is not None:
            covered.add(s._local_env_var)
        if s._secret_name is not None and not s._aliases:
            covered.add(s._secret_name)
        for src, dst in s._aliases.items():
            covered.add(dst or src)
    return covered


def resolve_ambient_chalk_secrets(
    existing_secrets: list[Secret],
    existing_env: dict[str, str] | None,
    client: Any,
) -> tuple[list[Secret], list[str]]:
    """Resolve ambient Chalk credentials and upsert them as Chalk secrets.

    Reads credentials with the same precedence as the local SDK (PR #160):
    explicit ``CHALK_*`` environment variables, falling back to
    ``~/.chalk.yml`` scoped to the current Chalk project. Each non-None
    value is upserted via ``EnvironmentSecretsService.UpsertSecret`` and
    returned as a ``Secret.from_env(...)`` reference.

    Names the user has already covered via an explicit ``Secret`` or
    ``env={...}`` are skipped, so the user's declarations always win.

    Parameters
    ----------
    existing_secrets
        Already-resolved (non-lazy) secrets the user provided.
    existing_env
        The container's ``env`` mapping, if any.
    client
        Connect client used to upsert secrets.

    Returns
    -------
    tuple[list[Secret], list[str]]
        ``(new_secret_refs, upserted_secret_names)`` — refs to append to the
        container's secret list, and the names of newly upserted secrets
        for cleanup tracking.
    """
    from chalkcompute._auth_config import load_token

    token = load_token(None, None, None, None)
    if token is None:
        logger.warning(
            "No ambient Chalk credentials found; ChalkClient() inside the "
            "container will fail unless you set CHALK_CLIENT_ID / "
            "CHALK_CLIENT_SECRET / CHALK_API_SERVER explicitly, run "
            "`chalk login`, or pass chalk_auth=False to suppress this warning."
        )
        return [], []

    name_to_value: dict[str, str | None] = {
        "CHALK_CLIENT_ID": token.clientId,
        "CHALK_CLIENT_SECRET": token.clientSecret,
        "CHALK_API_SERVER": token.apiServer,
        "CHALK_ENVIRONMENT_ID": token.activeEnvironment,
    }

    covered = _container_env_names_covered(existing_secrets, existing_env)

    new_refs: list[Secret] = []
    upserted: list[str] = []
    for name in _AMBIENT_CHALK_ENV_VARS:
        if name in covered:
            continue
        value = name_to_value.get(name)
        if value is None:
            continue
        _upsert_secret(client, name, value)
        upserted.append(name)
        new_refs.append(Secret.from_env(name))

    return new_refs, upserted


def delete_secrets(client: Any, names: list[str]) -> None:
    """Delete secrets by name via ``EnvironmentSecretsService``.

    Parameters
    ----------
    client
        Connect client used for the RPC.
    names
        Names of secrets to delete.
    """
    for name in names:
        client.rpc(_SECRETS_SVC, "DeleteSecret", {"name": name})
