"""Process-wide shared gRPC channel.

Every unary RPC in the SDK multiplexes over one long-lived HTTP/2 channel
instead of opening a TCP+TLS connection per call.
"""

from __future__ import annotations

import atexit
import dataclasses
import json
import logging
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
    """Channel options for the shared channel."""
    return [
        ("grpc.keepalive_time_ms", _KEEPALIVE_TIME_MS),
        ("grpc.keepalive_timeout_ms", _KEEPALIVE_TIMEOUT_MS),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.http2.max_pings_without_data", 0),
        ("grpc.max_send_message_length", _UNLIMITED_MESSAGE_SIZE),
        ("grpc.max_receive_message_length", _UNLIMITED_MESSAGE_SIZE),
        ("grpc.service_config", _SERVICE_CONFIG.to_json()),
    ]


# Following OpenTelemetry RPC semantic conventions:
# (https://opentelemetry.io/docs/specs/semconv/registry/attributes/rpc/)
_RPC_SPAN_NAME = "chalkcompute.rpc"
_RPC_SYSTEM_NAME_ATTRIBUTE = "rpc.system.name"
_RPC_METHOD_ATTRIBUTE = "rpc.method"
_RPC_RESPONSE_STATUS_CODE_ATTRIBUTE = "rpc.response.status_code"


class _TracingInterceptor(grpc.UnaryUnaryClientInterceptor):
    """Wrap each unary RPC in a ``chalkcompute.rpc`` span."""

    def intercept_unary_unary(self, continuation, client_call_details, request):
        method = (
            client_call_details.method
        )  # Example: /chalk.server.v1.ContainerService/GetContainer
        if isinstance(method, bytes):
            method = method.decode("utf-8", errors="replace")
        with get_tracer().start_as_current_span(_RPC_SPAN_NAME) as span:
            span.set_attribute(_RPC_SYSTEM_NAME_ATTRIBUTE, "grpc")
            span.set_attribute(_RPC_METHOD_ATTRIBUTE, method.lstrip("/"))
            call = continuation(client_call_details, request)
            code = call.code()  # blocks until the RPC completes; never raises
            span.set_attribute(_RPC_RESPONSE_STATUS_CODE_ATTRIBUTE, code.name)
            if code != grpc.StatusCode.OK:
                from opentelemetry.trace import StatusCode

                span.set_status(StatusCode.ERROR, code.name)
            return call


_channel: grpc.Channel | None = None


def get_channel(target: str, use_tls: bool) -> grpc.Channel:
    """Return the process-wide shared channel, dialing it on first use.

    Dials lazily (constructing a channel to an unreachable target is safe)
    and memoizes.
    """
    global _channel
    if _channel is None:
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
        _channel = grpc.intercept_channel(base, _TracingInterceptor())
    return _channel


def close_channel() -> None:
    """Close channel. Intended for process shutdown."""
    global _channel
    if _channel is not None:
        try:
            _channel.close()
        except Exception:
            logger.debug("Error closing channel", exc_info=True)
    _channel = None


atexit.register(close_channel)
