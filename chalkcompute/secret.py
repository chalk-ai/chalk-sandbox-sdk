"""Secret references for injecting secrets into Containers as environment variables."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Secret:
    """A reference to a Chalk secret or integration to inject as env vars.

    Use the factory methods to create instances:

        Secret.from_env("OPENAI_API_TOKEN")
        Secret.from_integration("prod_postgres")
        Secret.from_integration("prod_postgres", keys=["PGHOST", "PGPORT"])
    """

    _integration_name: str | None = field(default=None, repr=False)
    _secret_name: str | None = field(default=None, repr=False)
    _keys: list[str] = field(default_factory=list, repr=False)
    _aliases: dict[str, str] = field(default_factory=dict, repr=False)
    _prefix: str | None = field(default=None, repr=False)

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
        if self._secret_name:
            return f"Secret.from_env({self._secret_name!r})"
        return f"Secret.from_integration({self._integration_name!r})"
