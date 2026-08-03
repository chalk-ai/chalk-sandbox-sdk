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

The fix is the ``max_connections`` ceiling: descriptor use now tracks
*parallelism* and stops there, instead of tracking *request count* without
bound.  Set ``CHALK_SANDBOX_CONNECTION_POOLING=0`` to restore the old
unpooled behaviour.

HTTP/2 would go further and make the socket count constant, since it
multiplexes requests as streams over one connection.  It is not enabled,
because httpx's *synchronous* HTTP/2 backend is unsafe to drive from several
threads: ``httpcore``'s ``HTTP2Connection.handle_request`` calls
``get_next_available_stream_id()`` outside any lock and only takes
``_write_lock`` later, inside the HEADERS write, so two threads can write
frames in the opposite order to their stream ids.  Go's HTTP/2 server rejects
that with ``GOAWAY(PROTOCOL_ERROR, "InvalidNewStreamId")`` and drops the
connection along with every request on it -- measured against staging as clean
to 4 threads and failing from 8 (CHA-10029).  Retrying cannot help, since the
GOAWAY is provoked by our own frames.

Enabling h2 later is one argument to :func:`_build_client` plus the
``httpx[http2]`` extra; the right time is when the transport becomes async,
where a single event loop cannot interleave frame writes.
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

_POOLING_ENV_VAR = "CHALK_SANDBOX_CONNECTION_POOLING"

# httpx defaults to keepalive_expiry=5s, short enough that a container poll
# loop re-dials between iterations.  60s comfortably spans a boot.
_KEEPALIVE_EXPIRY_SECONDS = 60.0

# The fd ceiling.  Callers beyond this queue for a connection rather than
# opening one.
_MAX_CONNECTIONS = 64

_TRANSPORT_CONNECT_RETRIES = 3
_MAX_RETRY_ATTEMPTS = 4
_RETRY_BACKOFF_BASE_SECONDS = 0.1

# Failures that prove the request never reached the server, so re-issuing it
# cannot duplicate a side effect.
_UNSENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# The server closed a pooled connection while the request was in flight --
# normally a keep-alive the load balancer recycled, racing a request we had
# already written.  Whether it was processed first is not knowable from here,
# so this is only retried for RPCs the caller declared safe to repeat.
_AMBIGUOUS_ERRORS = (httpx.RemoteProtocolError,)

_lock = threading.Lock()
_clients: dict[str, httpx.Client] = {}
_owner_pid: int | None = None
_atexit_registered = False


def pooling_enabled() -> bool:
    """Return whether connection pooling is enabled."""
    raw = os.environ.get(_POOLING_ENV_VAR, "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _build_client() -> httpx.Client:
    limits = httpx.Limits(
        max_connections=_MAX_CONNECTIONS,
        # Kept equal to max_connections: a lower keepalive ceiling evicts live
        # connections under load, which is the churn we are here to remove.
        max_keepalive_connections=_MAX_CONNECTIONS,
        keepalive_expiry=_KEEPALIVE_EXPIRY_SECONDS,
    )
    # Limits must be set on the transport, not the client -- httpx ignores the
    # client-level argument once an explicit transport is passed.  ``http2``
    # would go here too; see the module docstring for why it is off.
    transport = httpx.HTTPTransport(
        retries=_TRANSPORT_CONNECT_RETRIES,
        limits=limits,
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
