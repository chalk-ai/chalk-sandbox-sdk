"""Secret references for injecting secrets into Containers as environment variables."""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from typing import Any

from protobuf import Oneof

from chalkcompute._gen.chalk.server.v1 import environment_secrets_connect
from chalkcompute._gen.chalk.server.v1 import environment_secrets_pb as secrets_pb


@dataclass(frozen=True)
class SecretScope:
    """The owning scope for secrets the SDK creates on a consumer's behalf.

    Secrets upserted with a scope belong to that consumer (a sandbox, scaling
    group, or function) rather than to the whole environment: they are never
    rendered into unrelated deployments.

    ``ref`` is the consumer's name, not an id. Reusing a name revives its
    previous scoped secret on the next upsert.

    Construct instances via the factory methods rather than instantiating
    directly.

    Examples
    --------
    >>> SecretScope.sandbox("my-container")
    >>> SecretScope.scaling_group("my-scaling-group")
    >>> SecretScope.function("my_function")
    """

    _type: str = field(repr=False)
    _ref: str = field(repr=False)

    @classmethod
    def sandbox(cls, ref: str) -> SecretScope:
        """Scope secrets to a sandbox/container, identified by its name."""
        return cls(_type="SECRET_SCOPE_TYPE_SANDBOX", _ref=ref)

    @classmethod
    def scaling_group(cls, ref: str) -> SecretScope:
        """Scope secrets to a scaling group, identified by its name."""
        return cls(_type="SECRET_SCOPE_TYPE_SCALING_GROUP", _ref=ref)

    @classmethod
    def function(cls, ref: str) -> SecretScope:
        """Scope secrets to a remote function or class, identified by its name."""
        return cls(_type="SECRET_SCOPE_TYPE_FUNCTION", _ref=ref)

    def _to_proto_dict(self) -> dict[str, str]:
        """Serialize to the dict shape expected by the ``SecretScope`` protobuf."""
        return {"type": self._type, "ref": self._ref}

    def _to_proto(self) -> secrets_pb.SecretScope:
        return secrets_pb.SecretScope(
            type=secrets_pb.SecretScopeType[
                self._type.removeprefix("SECRET_SCOPE_TYPE_")
            ],
            ref=self._ref,
        )

    def __repr__(self) -> str:
        return f"SecretScope(type={self._type!r}, ref={self._ref!r})"


@dataclass(frozen=True)
class Secret:
    """A reference to a Chalk secret or integration to inject as env vars.

    Construct instances via the factory methods rather than instantiating
    directly.

    Examples
    --------
    >>> Secret.from_chalk_env("OPENAI_API_TOKEN")
    >>> Secret.from_chalk_integration("prod_postgres")
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
    def from_chalk_env(
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
    def from_chalk_integration(
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
    def from_env(
        cls,
        name: str,
        *,
        alias: str | None = None,
        prefix: str | None = None,
    ) -> Secret:
        """Deprecated alias for :meth:`from_chalk_env`.

        .. deprecated::
            Use :meth:`from_chalk_env` instead.
        """
        warnings.warn(
            "Secret.from_env is deprecated; use Secret.from_chalk_env instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return cls.from_chalk_env(name, alias=alias, prefix=prefix)

    @classmethod
    def from_integration(
        cls,
        name: str,
        *,
        keys: list[str] | None = None,
        aliases: dict[str, str] | None = None,
        prefix: str | None = None,
    ) -> Secret:
        """Deprecated alias for :meth:`from_chalk_integration`.

        .. deprecated::
            Use :meth:`from_chalk_integration` instead.
        """
        warnings.warn(
            "Secret.from_integration is deprecated; "
            "use Secret.from_chalk_integration instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return cls.from_chalk_integration(
            name, keys=keys, aliases=aliases, prefix=prefix
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
            return (
                f"Secret.from_local_env({self._local_env_var!r}, env_var_name={name!r})"
            )
        if self._secret_name is not None:
            return f"Secret.from_chalk_env({self._secret_name!r})"
        return f"Secret.from_chalk_integration({self._integration_name!r})"


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
    scope: SecretScope | None = None,
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
    scope
        Owning scope for the upserted secrets. When set, secrets belong to
        that consumer instead of the whole environment.

    Returns
    -------
    tuple[list[Secret], list[str]]
        The resolved secrets (lazy ones replaced with ``from_chalk_env`` equivalents)
        and the names of secrets that were upserted.

    Raises
    ------
    ValueError
        If a referenced local environment variable is not set. Any secrets
        already upserted before the failure are not rolled back.
    """
    resolved: list[Secret] = []
    upserted_names: list[str] = []

    for secret in secrets:
        if secret._local_env_var is not None:
            local_var = secret._local_env_var
            value = os.environ.get(local_var)
            if value is None:
                raise ValueError(f"Local environment variable {local_var!r} is not set")
            remote_name = secret._env_var_name or local_var
            _upsert_secret(client, remote_name, value, scope)
            upserted_names.append(remote_name)
            resolved.append(Secret.from_chalk_env(remote_name))

        elif secret._local_env_file is not None:
            env_vars = _parse_env_file(secret._local_env_file)
            for name, value in env_vars.items():
                _upsert_secret(client, name, value, scope)
                upserted_names.append(name)
                resolved.append(Secret.from_chalk_env(name))

        else:
            resolved.append(secret)

    return resolved, upserted_names


def _upsert_secret(
    client: Any, name: str, value: str, scope: SecretScope | None
) -> None:
    """Upsert a secret via ``EnvironmentSecretsService``.

    Parameters
    ----------
    client
        Connect client used for the RPC.
    name
        Name of the secret to upsert.
    value
        Literal secret value.
    scope
        Owning scope for the secret, or ``None`` for environment-wide.

    Raises
    ------
    RuntimeError
        If the upsert RPC fails.
    """
    stub = client.stub(environment_secrets_connect.EnvironmentSecretsServiceClientSync)
    client.call(
        stub.upsert_secret,
        secrets_pb.UpsertSecretRequest(
            name=name,
            config=secrets_pb.SecretConfigValue(value=Oneof("literal", value)),
            scope=None if scope is None else scope._to_proto(),
        ),
    )
