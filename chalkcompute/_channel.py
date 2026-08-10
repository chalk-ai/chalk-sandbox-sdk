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
import dataclasses
import json
import logging
import os
import threading
from typing import Any

import grpc

from chalkcompute._tracing import get_tracer

logger = logging.getLogger(__name__)

# Ping every 60s while the channel is idle so LB/NATs keep connection alive,.
_KEEPALIVE_TIME_MS = 60_000
# Fail the connection if a ping goes unacked for 5s.
_KEEPALIVE_TIMEOUT_MS = 5_000
# gRPC channel options interpret -1 as "no limit".
_UNLIMITED_MESSAGE_SIZE = -1


@dataclasses.dataclass(frozen=True)
class _RetryPolicy:
    """Transparent retries for transient connection loss.

    Serialized into the channel's service config:
    https://github.com/grpc/grpc/blob/master/doc/service_config.md
    """

    # Backoff retry waits are: 0.1s, 0.2s, 0.4s, 0.8s:
    max_attempts: int = 5
    initial_backoff: str = "0.1s"
    max_backoff: str = "1s"
    backoff_multiplier: int = 2
    # Retry transport level failures; avoid retrying after reaching server:
    retryable_status_codes: tuple[str, ...] = ("UNAVAILABLE",)

    def to_dict(self) -> dict[str, Any]:
        return {
            "maxAttempts": self.max_attempts,
            "initialBackoff": self.initial_backoff,
            "maxBackoff": self.max_backoff,
            "backoffMultiplier": self.backoff_multiplier,
            "retryableStatusCodes": list(self.retryable_status_codes),
        }


@dataclasses.dataclass(frozen=True)
class _ServiceConfig:
    """gRPC service config applying ``retry_policy`` to every method."""

    retry_policy: _RetryPolicy = _RetryPolicy()

    def to_json(self) -> str:
        # https://github.com/grpc/grpc/blob/master/doc/service_config.md
        return json.dumps(
            {
                "methodConfig": [
                    {"name": [{}], "retryPolicy": self.retry_policy.to_dict()}
                ]
            }
        )


_SERVICE_CONFIG = _ServiceConfig()


def default_channel_options() -> list[tuple[str, Any]]:
    """Channel options for the shared registry."""
    return [
        ("grpc.keepalive_time_ms", _KEEPALIVE_TIME_MS),
        ("grpc.keepalive_timeout_ms", _KEEPALIVE_TIMEOUT_MS),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.http2.max_pings_without_data", 0),
        ("grpc.max_send_message_length", _UNLIMITED_MESSAGE_SIZE),
        ("grpc.max_receive_message_length", _UNLIMITED_MESSAGE_SIZE),
        ("grpc.service_config", _SERVICE_CONFIG.to_json()),
    ]


class _TracingInterceptor(grpc.UnaryUnaryClientInterceptor):
    """Wrap each unary RPC in a ``chalkcompute.rpc`` span.

    Preserves the span name and ``rpc.service``/``rpc.method`` attributes the
    Connect-JSON transport emitted, so dashboards keyed on them keep working.
    """

    def intercept_unary_unary(self, continuation, client_call_details, request):
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


def get_channel(target: str, use_tls: bool) -> grpc.Channel:
    """Return the process-wide shared channel for ``(target, use_tls)``.

    Dials lazily (constructing a channel to an unreachable target is safe)
    and memoizes.
    """
    global _owner_pid
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
            # Create channel:
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


atexit.register(close_all)
