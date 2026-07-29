"""Helpers for Chalk web-identity JWT authentication."""

from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


WEB_IDENTITY_TOKEN_FILE_ENV_VAR = "CHALK_WEB_IDENTITY_TOKEN_FILE"


class WebIdentityTokenError(ValueError):
    """Raised when a Chalk web-identity token file cannot be used."""


@dataclass(frozen=True)
class WebIdentityToken:
    """A directly usable Chalk JWT and the environment encoded in it."""

    value: str
    environment_id: str


def web_identity_token_file_from_env() -> str | None:
    """Return the configured token file path, if any."""
    path = os.environ.get(WEB_IDENTITY_TOKEN_FILE_ENV_VAR)
    if path is None:
        return None
    path = path.strip()
    return path or None


def read_web_identity_token(path: str) -> WebIdentityToken:
    """Read and decode a Chalk identity JWT."""
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise WebIdentityTokenError(
            f"Unable to read {WEB_IDENTITY_TOKEN_FILE_ENV_VAR} file {path!r}: {exc}"
        ) from exc

    if not token:
        raise WebIdentityTokenError(
            f"{WEB_IDENTITY_TOKEN_FILE_ENV_VAR} file {path!r} is empty"
        )

    claims = _decode_jwt_claims(token, path=path)
    environment_id = claims.get("environment_id")
    if not isinstance(environment_id, str) or not environment_id:
        raise WebIdentityTokenError(
            f"JWT in {WEB_IDENTITY_TOKEN_FILE_ENV_VAR} file {path!r} "
            "has no non-empty environment_id claim"
        )
    return WebIdentityToken(value=token, environment_id=environment_id)


def _decode_jwt_claims(token: str, *, path: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise WebIdentityTokenError(
            f"JWT in {WEB_IDENTITY_TOKEN_FILE_ENV_VAR} file {path!r} is malformed"
        )

    payload = parts[1]
    try:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebIdentityTokenError(
            f"JWT in {WEB_IDENTITY_TOKEN_FILE_ENV_VAR} file {path!r} "
            "has an invalid payload"
        ) from exc
    if not isinstance(claims, dict):
        raise WebIdentityTokenError(
            f"JWT in {WEB_IDENTITY_TOKEN_FILE_ENV_VAR} file {path!r} "
            "does not contain an object payload"
        )
    return claims
