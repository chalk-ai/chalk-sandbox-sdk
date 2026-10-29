"""Provider-neutral sources for Chalk workload identity tokens."""

from __future__ import annotations

import tempfile
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chalkcompute._connect import ConnectClient


class WorkloadIdentityTokenError(RuntimeError):
    """Raised when a requested workload identity token cannot be provided."""


@dataclass(frozen=True)
class OIDCToken:
    """Opaque OIDC token and server-reported metadata."""

    value: str
    expires_at: datetime | None
    issuer: str | None
    subject: str | None


class WorkloadIdentityTokenSource:
    """Retrieve short-lived Chalk OIDC tokens for one audience.

    The source caches tokens with a server-reported expiry and refreshes them
    when they do not meet the requested minimum remaining lifetime. Tokens
    without an expiry are returned only when ``min_ttl`` is zero and are not
    cached.

    Parameters
    ----------
    audience
        Audience expected by the external service that will receive the token.
    connect_client
        Optional initialized ConnectClient to reuse. The source does not own or
        close an injected client. When omitted, a ConnectClient is created lazily
        on the first token request using ambient Chalk authentication.

    Examples
    --------
    >>> with WorkloadIdentityTokenSource("api.example.com") as source:
    ...     token = source.get_token(min_ttl=timedelta(seconds=60))
    ...     token.value  # pass to a provider-specific integration
    'eyJ...'
    """

    def __init__(
        self,
        audience: str,
        *,
        connect_client: ConnectClient | None = None,
    ) -> None:
        if not isinstance(audience, str) or not audience.strip():
            raise ValueError("audience cannot be empty")
        self._audience = audience
        self._connect_client = connect_client
        self._owns_connect_client = connect_client is None
        self._cached_token: OIDCToken | None = None
        self._lock = threading.Lock()

    def get_token(
        self,
        *,
        min_ttl: timedelta = timedelta(0),
        timeout: float = 30,
    ) -> OIDCToken:
        """Return a token with at least ``min_ttl`` remaining when possible.

        A cached token is reused only when its server-reported expiry leaves at
        least ``min_ttl``. Otherwise, the token source requests a new token. If
        Chalk does not provide an expiry, a positive ``min_ttl`` cannot be
        guaranteed and raises :class:`WorkloadIdentityTokenError`.

        Parameters
        ----------
        min_ttl
            Minimum remaining token lifetime. Defaults to zero.
        timeout
            Timeout in seconds for the token request.
        """
        if not isinstance(min_ttl, timedelta):
            raise TypeError("min_ttl must be a timedelta")
        if min_ttl < timedelta(0):
            raise ValueError("min_ttl cannot be negative")

        with self._lock:
            now = datetime.now(UTC)
            cached = self._cached_token
            if (
                cached is not None
                and cached.expires_at is not None
                and cached.expires_at >= now + min_ttl
            ):
                return cached

            if self._connect_client is None:
                self._connect_client = ConnectClient()

            response = self._connect_client._get_workload_identity_token_response(
                self._audience,
                timeout=timeout,
            )
            expires_at = (
                datetime.fromtimestamp(response.expires_at.seconds, tz=UTC)
                + timedelta(microseconds=response.expires_at.nanos // 1_000)
                if response.expires_at is not None
                else None
            )
            token = OIDCToken(
                value=response.token,
                expires_at=expires_at,
                issuer=response.issuer or None,
                subject=response.subject or None,
            )

            now = datetime.now(UTC)
            if expires_at is None and min_ttl > timedelta(0):
                raise WorkloadIdentityTokenError(
                    "Chalk did not return an expiry; cannot guarantee min_ttl"
                )
            if expires_at is not None and expires_at < now + min_ttl:
                raise WorkloadIdentityTokenError(
                    "Chalk returned a token that does not meet min_ttl"
                )

            self._cached_token = token if expires_at is not None else None
            return token

    @contextmanager
    def token_file(
        self,
        *,
        min_ttl: timedelta = timedelta(0),
        timeout: float = 30,
    ) -> Generator[Path, None, None]:
        """Yield a private temporary file containing an OIDC token.

        Uses :meth:`get_token` to satisfy ``min_ttl`` at acquisition time. The
        file is a snapshot and is not refreshed while the context is open. It
        is closed before being yielded so another process can read it, and is
        deleted when the context exits, including when the consumer raises.

        The file is created with owner-only read/write permissions (``0600``)
        inside a private temporary directory. Call this method inside the
        workload that will consume the token so its value stays there.
        """
        token = self.get_token(min_ttl=min_ttl, timeout=timeout)
        with tempfile.TemporaryDirectory(prefix="chalk-oidc-") as directory:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, delete=False
            ) as token_file:
                token_file.write(token.value)
            yield Path(token_file.name)

    def close(self) -> None:
        """Clear cached token data and close an internally owned client."""
        with self._lock:
            self._cached_token = None
            if self._owns_connect_client and self._connect_client is not None:
                self._connect_client.close()
                self._connect_client = None

    def __enter__(self) -> WorkloadIdentityTokenSource:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
