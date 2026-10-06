"""Normalize deployment environment variables into literals and secret references."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from chalkcompute._naming import validate_env
from chalkcompute._secret import Secret


def normalize_env(
    env: Mapping[str, str | Secret] | None,
    secrets: Sequence[Secret] | None = None,
) -> tuple[dict[str, str], list[Secret]]:
    """Use each env key as the destination name for a single secret value.

    Local env references remain lazy. Bulk references belong in ``secrets``;
    an integration reference in ``env`` must select exactly one key.
    Explicit secrets retain their order, followed by references from ``env``.
    Neither the input collections nor their Secret objects are mutated.
    """
    # Validate keys and literal values together, preserving the helpful error
    # for None from os.getenv without trying to serialize Secret objects.
    validate_env(
        {
            key: "" if isinstance(value, Secret) else value
            for key, value in (env or {}).items()
        }
    )
    literals: dict[str, str] = {}
    references = list(secrets or [])
    for key, value in (env or {}).items():
        if isinstance(value, str):
            literals[key] = value
        elif value._local_env_var is not None:
            references.append(replace(value, _env_var_name=key))
        elif value._secret_name is not None:
            references.append(
                replace(value, _aliases={value._secret_name: key}, _prefix=None)
            )
        elif value._integration_name is not None and len(value._keys) == 1:
            references.append(
                replace(value, _aliases={value._keys[0]: key}, _prefix=None)
            )
        else:
            raise ValueError(
                f"env[{key!r}] must reference a single secret value. "
                "For integrations, select exactly one key with keys=[...]. "
                "Pass bulk integrations and local env files via secrets=[...] instead."
            )
    return literals, references
