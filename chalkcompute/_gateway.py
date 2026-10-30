"""OpenAI-compatible access to Chalk's model gateway."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

from chalkcompute import _tracing
from chalkcompute._connect import ConnectClient

if TYPE_CHECKING:
    from openai import OpenAI  # ty: ignore[unresolved-import]

_REFRESH_SECONDS = 30 * 60
_MANAGED_OPTIONS = {
    "api_key",
    "admin_api_key",
    "base_url",
    "http_client",
    "provider",
    "workload_identity",
}


class _GatewayAuth:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connection: ConnectClient | None = None
        self._issued_at = 0.0
        _, self.base_url, _ = self.credentials()

    def credentials(self) -> tuple[str, str, str]:
        with self._lock:
            now = time.monotonic()
            try:
                if (
                    self._connection is None
                    or now - self._issued_at >= _REFRESH_SECONDS
                ):
                    if self._connection is not None:
                        self._connection.close()
                    self._connection = None
                    self._connection = ConnectClient()
                    self._issued_at = now
                # This also re-reads rotating workload identity tokens as needed.
                self._connection._ensure_auth()
                token = self._connection._access_token
                server = self._connection._api_server
                environment = self._connection._env_id or ""
                if not token or not server:
                    raise RuntimeError(
                        "Chalk credentials did not include a token and API server"
                    )
            except Exception as exc:
                if self._connection is not None:
                    self._connection.close()
                self._connection = None
                raise RuntimeError(
                    "Chalk gateway authentication failed. Configure Chalk credentials "
                    "locally or deploy the function with chalk_identity=True."
                ) from exc
            return token, f"{server.rstrip('/')}/v1/router", environment

    def prepare_request(self, request: Any) -> None:
        # A client copy with a different base_url must never send Chalk credentials
        # to another service. Redirects are disabled for the same reason.
        if not str(request.url).startswith(self.base_url + "/"):
            raise ValueError("This client can only send requests to its Chalk gateway")
        token, base_url, environment = self.credentials()
        if base_url != self.base_url:
            raise ValueError("Chalk API server changed; create a new gateway client")
        for key in ("traceparent", "tracestate", "baggage", "X-Chalk-Agent-Session-Id"):
            request.headers.pop(key, None)
        request.headers["Authorization"] = f"Bearer {token}"
        request.headers["X-Chalk-Env-Id"] = environment
        request.headers.update(_tracing.trace_headers())
        session = _tracing.current_session_id()
        if session:
            request.headers["X-Chalk-Agent-Session-Id"] = session


def openai_client(**options: Any) -> OpenAI:
    """Return a synchronous OpenAI client authenticated to Chalk's model gateway.

    Install ``chalkcompute[gateway]``. Uses the same ambient Chalk credentials
    as the rest of the SDK; deployed functions need ``chalk_identity=True``.
    Provider keys and ``OPENAI_BASE_URL`` are not used. Requests go to
    ``/v1/router`` on the authenticated Chalk API server, with the active
    environment and current trace/session attached separately to each request.
    Credentials refresh during the client's lifetime, including on retries.

    Other OpenAI constructor options, such as ``timeout`` and ``max_retries``,
    can be supplied when needed; their OpenAI defaults are otherwise preserved.
    Authentication, endpoint, and HTTP transport are managed by Chalk and
    cannot be overridden here. Close the returned client or use it as a context
    manager. Only HTTP endpoints supported by the gateway are available; this
    does not add support for every OpenAI API or for Realtime WebSockets.

    Examples
    --------
    >>> import chalkcompute as cc
    >>> with cc.openai_client() as client:
    ...     response = client.chat.completions.create(
    ...         model="openai/gpt-4.1-mini-2025-04-14",
    ...         messages=[{"role": "user", "content": "Hello"}],
    ...     )
    """
    unsupported = _MANAGED_OPTIONS.intersection(options)
    if unsupported:
        raise TypeError(
            f"Chalk manages these client options: {', '.join(sorted(unsupported))}"
        )
    try:
        from openai import DefaultHttpxClient, OpenAI
    except ImportError as exc:
        raise ImportError(
            "Install chalkcompute[gateway] to use cc.openai_client()"
        ) from exc

    auth = _GatewayAuth()
    http_client = DefaultHttpxClient(
        event_hooks={"request": [auth.prepare_request]},
        follow_redirects=False,
    )
    try:
        # The hook supplies the actual credential immediately before each send.
        return OpenAI(
            api_key="chalk-gateway",
            base_url=auth.base_url,
            http_client=http_client,
            **options,
        )
    except BaseException:
        http_client.close()
        raise
