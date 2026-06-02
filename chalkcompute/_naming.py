"""DNS-safe name sanitization for containers, scaling groups, and functions."""

from __future__ import annotations

import re
from collections.abc import Mapping

_DNS_LABEL_RE = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")


def validate_dns_label(name: str) -> None:
    """Validate that ``name`` is a well-formed DNS label.

    Must be 1–63 chars, lowercase letters/digits/hyphens only, and must
    start with a letter and end with an alphanumeric character.

    Parameters
    ----------
    name
        Name to validate.

    Raises
    ------
    ValueError
        If the name is not a valid DNS label.
    """
    if not _DNS_LABEL_RE.match(name):
        raise ValueError(
            f"Invalid name {name!r}: must be a valid DNS label "
            "(lowercase letters, digits, and hyphens only, 1-63 chars, "
            "must start with a letter and end with an alphanumeric character)."
        )


def sanitize_dns_label(name: str, max_length: int = 63) -> str:
    """Sanitize a string into a valid DNS label.

    Lowercases all characters, replaces underscores and dots with hyphens,
    strips characters that aren't alphanumeric or hyphens, collapses
    consecutive hyphens, strips leading/trailing hyphens, and truncates
    to ``max_length``.

    Parameters
    ----------
    name
        Raw name to sanitize.
    max_length
        Maximum length of the returned label.

    Returns
    -------
    str
        A valid DNS label.

    Raises
    ------
    ValueError
        If the sanitized result is empty or starts with a digit.
    """
    name = name.lower()
    name = name.replace("_", "-").replace(".", "-")
    name = re.sub(r"[^a-z0-9-]", "", name)
    name = re.sub(r"-{2,}", "-", name)
    name = name.strip("-")
    name = name[:max_length].rstrip("-")
    if not name or not name[0].isalpha():
        raise ValueError(
            f"Cannot create a valid DNS label from {name!r}: "
            "the name must contain at least one letter and not start with a digit."
        )
    return name


def validate_env(env: Mapping[str, str] | None) -> None:
    """Validate that an ``env`` mapping has non-empty string keys and string values.

    Every consumer that accepts ``env`` (functions, classes, containers,
    scaling groups, sandboxes, images) ultimately serializes it into a proto
    ``map<string, string>`` (``envVars`` / per-exec ``env``). A non-string
    value — most commonly ``None`` from ``os.getenv("X")`` when ``X`` is unset
    — serializes to JSON ``null`` and is rejected server-side with an opaque
    ``invalid value for string field value: null`` error. Catch it here so the
    failure surfaces client-side, naming the offending key and the usual cause.

    Parameters
    ----------
    env
        The mapping to validate. ``None`` and empty mappings are accepted.

    Raises
    ------
    ValueError
        If a key is not a non-empty string.
    TypeError
        If a value is not a string.
    """
    if not env:
        return
    for k in env:
        if not isinstance(k, str) or not k:
            raise ValueError(f"env keys must be non-empty strings, got {k!r}")
    bad = {k: v for k, v in env.items() if not isinstance(v, str)}
    if bad:
        offenders = ", ".join(
            f"{k!r}: {type(v).__name__}" for k, v in sorted(bad.items())
        )
        raise TypeError(
            "env values must be strings; got non-string for "
            f"{{{offenders}}}. A common cause is passing os.getenv('X') for an "
            "unset variable (which returns None) — set the variable, supply a "
            "string default, or pass it as a Secret instead."
        )
