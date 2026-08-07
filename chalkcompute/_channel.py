"""Process-wide shared gRPC channel registry.

Every unary RPC in the SDK multiplexes over one long-lived HTTP/2 channel per
``(target, tls)`` pair instead of opening a TCP+TLS connection per call

grpc-python's C-core channel is thread-safe for concurrent sync callers, so
worker threads share it directly.

Environment selection stays out of the cache key: ``x-chalk-env-id`` is
per-call metadata, so one channel serves every environment on a target.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from typing import Any

import grpc

logger = logging.getLogger(__name__)

# Transparent retries for transient connection loss.
_SERVICE_CONFIG = json.dumps(
    {
        "methodConfig": [
            {
                "name": [{}],
                "retryPolicy": {
                    "maxAttempts": 5,
                    "initialBackoff": "0.1s",
                    "maxBackoff": "1s",
                    "backoffMultiplier": 2,
                    "retryableStatusCodes": ["UNAVAILABLE"],
                },
            }
        ]
    }
)


def default_channel_options() -> list[tuple[str, Any]]:
    """Channel options for the shared registry.

    Keepalive pings keep idle channels from being silently dropped by LBs and
    NATs (poll loops sleep between unary calls, so the connection is idle most
    of its life); unlimited message sizes match the volume client's needs so
    it can share the channel later.
    """
    return [
        ("grpc.keepalive_time_ms", 60_000),
        ("grpc.keepalive_timeout_ms", 5_000),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.http2.max_pings_without_data", 0),
        ("grpc.max_send_message_length", -1),
        ("grpc.max_receive_message_length", -1),
        ("grpc.service_config", _SERVICE_CONFIG),
    ]


class _TracingInterceptor(grpc.UnaryUnaryClientInterceptor):
    """Wrap each unary RPC in a ``chalkcompute.rpc`` span.

    Preserves the span name and ``rpc.service``/``rpc.method`` attributes the
    Connect-JSON transport emitted, so dashboards keyed on them keep working.
    """

    def intercept_unary_unary(self, continuation, client_call_details, request):
        from chalkcompute._tracing import get_tracer

        method = client_call_details.method
        if isinstance(method, bytes):
            method = method.decode("utf-8", errors="replace")
        service, _, method_name = method.lstrip("/").partition("/")
        with get_tracer().start_as_current_span("chalkcompute.rpc") as span:
            span.set_attribute("rpc.system", "grpc")
            span.set_attribute("rpc.service", service)
            span.set_attribute("rpc.method", method_name)
            call = continuation(client_call_details, request)
            code = call.code()  # blocks until the RPC completes; never raises
            span.set_attribute("rpc.grpc.status_code", code.name)
            if code != grpc.StatusCode.OK:
                from opentelemetry.trace import StatusCode

                span.set_status(StatusCode.ERROR, code.name)
            return call


_lock = threading.Lock()
_channels: dict[tuple[str, bool], grpc.Channel] = {}
_owner_pid: int | None = None
_atexit_registered = False


def get_channel(target: str, use_tls: bool) -> grpc.Channel:
    """Return the process-wide shared channel for ``(target, use_tls)``.

    Dials lazily (constructing a channel to an unreachable target is safe)
    and memoizes.
    """
    global _owner_pid, _atexit_registered
    cache_key = (target, use_tls)
    with _lock:
        pid = os.getpid()
        if _owner_pid != pid:
            # Forked child: parent owns these sockets — drop without closing.
            if _owner_pid is not None:
                logger.debug(
                    "pid changed (%s -> %s); dropping shared channels", _owner_pid, pid
                )
            _channels.clear()
            _owner_pid = pid
        channel = _channels.get(cache_key)
        if channel is None:
            base = (
                grpc.secure_channel(
                    target,
                    grpc.ssl_channel_credentials(),
                    options=default_channel_options(),
                )
                if use_tls
                else grpc.insecure_channel(target, options=default_channel_options())
            )
            channel = grpc.intercept_channel(base, _TracingInterceptor())
            _channels[cache_key] = channel
            if not _atexit_registered:
                atexit.register(close_all)
                _atexit_registered = True
        return channel


def close_all() -> None:
    """Close every channel this process owns and empty the registry."""
    with _lock:
        if _owner_pid == os.getpid():
            for channel in _channels.values():
                try:
                    channel.close()
                except Exception:  # noqa: BLE001 - best-effort shutdown
                    logger.debug("error closing shared channel", exc_info=True)
        _channels.clear()
