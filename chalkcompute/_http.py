"""Process-shared pooled HTTP transport for Chalk API calls.

Every Connect RPC used to go out as a bare module-level ``httpx.post``, which
builds a client, opens a connection, sends one request and throws the
connection away.  At load that is a DNS lookup, a TCP handshake, a TLS
handshake and a file descriptor *per RPC*, which exhausts the process fd limit
(``EMFILE``, and with it name resolution) long before the server notices.

Pooling has to be process-wide rather than per-client: ``Container``,
``Sandbox``, ``Function``, ``Cls``, ``ScalingGroup`` and ``VolumeClient`` each
construct their own :class:`~chalkcompute._connect.ConnectClient`, so a pool
owned by one of those would still leave a few hundred pools open.  Clients here
are memoised per origin and shared by every caller in the process.

Pooling is what fixes the bug: sockets now scale with *parallelism* rather than
with *request count*, and it was request count that exhausted the fd table.  A
poll loop can run for minutes on one connection instead of re-dialing.

HTTP/2 would go further and make the socket count constant, since it multiplexes
requests as streams over a single connection.  It is nonetheless **off by
default**, because httpx's synchronous HTTP/2 backend is not safe to drive from
several threads: httpcore does not hold one lock across "allocate the next
stream id" and "write the HEADERS frame", so concurrent callers can put frames
on the wire out of order.  Go's HTTP/2 server rejects that with
``GOAWAY(PROTOCOL_ERROR, "InvalidNewStreamId")`` and tears the connection down,
taking every in-flight request with it.  Measured against staging, this is
clean up to 4 threads and starts failing at 8 (see CHA-10029).

Retrying cannot paper over it: the GOAWAY is provoked by our own invalid frames,
so the retry hits the same race.  ``CHALK_SANDBOX_HTTP2=1`` enables h2 for
single-threaded callers, who do benefit.  An async client would sidestep the
race entirely — one event-loop thread cannot interleave frame writes — which is
the strongest argument for the deferred native-async transport.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_HTTP2_ENV_VAR = "CHALK_SANDBOX_HTTP2"
_POOLING_ENV_VAR = "CHALK_SANDBOX_CONNECTION_POOLING"
_MAX_CONNECTIONS_ENV_VAR = "CHALK_SANDBOX_MAX_CONNECTIONS"
_KEEPALIVE_EXPIRY_ENV_VAR = "CHALK_SANDBOX_KEEPALIVE_EXPIRY"

# httpx defaults to keepalive_expiry=5s, short enough that a poll loop keeps
# re-dialing between iterations.  60s comfortably spans a container boot.
_DEFAULT_KEEPALIVE_EXPIRY_SECONDS = 60.0
_DEFAULT_MAX_CONNECTIONS = 64

# Connect-level retries handled inside the transport itself.
_TRANSPORT_CONNECT_RETRIES = 3

_MAX_RETRY_ATTEMPTS = 4
_RETRY_BACKOFF_BASE_SECONDS = 0.1

# Failures that prove the request never reached the server, so re-issuing it
# cannot duplicate a side effect.
_UNSENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# The server closed a pooled connection while the request was in flight —
# normally a keep-alive the load balancer decided to recycle, racing a request
# we had already written.  Whether the server processed it first is not knowable
# from the client, so this is only retried for RPCs the caller has declared safe
# to repeat.
_AMBIGUOUS_ERRORS = (httpx.RemoteProtocolError,)

_lock = threading.Lock()
_clients: dict[str, httpx.Client] = {}
_owner_pid: int | None = None
_atexit_registered = False


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r", name, raw)
        return default


def http2_enabled() -> bool:
    """Return whether HTTP/2 negotiation is enabled for pooled clients.

    Off by default — see the module docstring for why multi-threaded h2 is
    unsafe with httpx's sync backend.
    """
    return _env_flag(_HTTP2_ENV_VAR, False)


def pooling_enabled() -> bool:
    """Return whether connection pooling is enabled."""
    return _env_flag(_POOLING_ENV_VAR, True)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _build_client() -> httpx.Client:
    max_connections = int(
        _env_number(_MAX_CONNECTIONS_ENV_VAR, _DEFAULT_MAX_CONNECTIONS)
    )
    keepalive_expiry = _env_number(
        _KEEPALIVE_EXPIRY_ENV_VAR, _DEFAULT_KEEPALIVE_EXPIRY_SECONDS
    )
    limits = httpx.Limits(
        max_connections=max_connections,
        # Kept equal to max_connections: a lower keepalive ceiling evicts live
        # connections under load, which is the churn we are here to remove.
        max_keepalive_connections=max_connections,
        keepalive_expiry=keepalive_expiry,
    )
    # Limits and http2 must be set on the transport, not the client — httpx
    # ignores the client-level arguments once an explicit transport is passed.
    transport = httpx.HTTPTransport(
        retries=_TRANSPORT_CONNECT_RETRIES,
        limits=limits,
        http2=http2_enabled(),
    )
    return httpx.Client(transport=transport)


def get_client(url: str) -> httpx.Client:
    """Return the process-shared pooled client for ``url``'s origin.

    Clients are memoised per scheme/host/port.  After a fork the child gets a
    fresh client: the inherited sockets belong to the parent and must not be
    used (or closed) from here.
    """
    global _owner_pid, _atexit_registered

    origin = _origin(url)
    pid = os.getpid()
    with _lock:
        if _owner_pid != pid:
            # Drop, never close: these sockets are still the parent's.
            _clients.clear()
            _owner_pid = pid
        client = _clients.get(origin)
        if client is None:
            client = _build_client()
            _clients[origin] = client
        if not _atexit_registered:
            atexit.register(close_all)
            _atexit_registered = True
        return client


def close_all() -> None:
    """Close every pooled client owned by this process."""
    with _lock:
        if _owner_pid is not None and _owner_pid != os.getpid():
            _clients.clear()
            return
        while _clients:
            _, client = _clients.popitem()
            try:
                client.close()
            except Exception as exc:  # pragma: no cover - best-effort cleanup
                logger.debug("Error closing pooled HTTP client: %s", exc)


def request(
    method: str,
    url: str,
    *,
    retry_safe: bool = False,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request over the process-shared connection pool.

    Parameters
    ----------
    method
        HTTP method.
    url
        Absolute request URL.  Its origin selects the pooled client.
    retry_safe
        Whether re-issuing this request is free of side effects.  Connection
        failures that provably predate the request are always retried; failures
        that might have been processed server-side are retried only when this
        is ``True``.
    **kwargs
        Passed through to httpx (``json``, ``headers``, ``timeout``, ...).

    Returns
    -------
    httpx.Response
        The response, exactly as the unpooled call would have returned it.
    """
    if not pooling_enabled():
        return httpx.request(method, url, **kwargs)

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRY_ATTEMPTS):
        client = get_client(url)
        try:
            return client.request(method, url, **kwargs)
        except _UNSENT_ERRORS as exc:
            last_exc = exc
        except _AMBIGUOUS_ERRORS as exc:
            if not retry_safe:
                raise
            last_exc = exc

        if attempt == _MAX_RETRY_ATTEMPTS - 1:
            break
        delay = _RETRY_BACKOFF_BASE_SECONDS * 2**attempt
        logger.warning(
            "Retrying %s %s after %s (attempt %d/%d, sleeping %.2fs)",
            method,
            url,
            type(last_exc).__name__,
            attempt + 1,
            _MAX_RETRY_ATTEMPTS,
            delay,
        )
        time.sleep(delay)

    assert last_exc is not None
    raise last_exc


def post(url: str, *, retry_safe: bool = False, **kwargs: Any) -> httpx.Response:
    """``POST`` over the process-shared connection pool."""
    return request("POST", url, retry_safe=retry_safe, **kwargs)
