"""Python wrapper around the native remote-call client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


try:
    from chalkcompute._remote_call_native import RemoteCallClient as _NativeRemoteCallClient
except ImportError as exc:  # pragma: no cover - exercised in source checkouts
    _NativeRemoteCallClient = None
    _NATIVE_IMPORT_ERROR: ImportError | None = exc
else:
    _NATIVE_IMPORT_ERROR = None


class RemoteCallClientUnavailable(RuntimeError):
    """Raised when the native remote-call extension is not installed."""


@dataclass(frozen=True)
class RemoteCallPollResult:
    status: int
    status_name: str
    chunks: list[bytes]
    cursor: str
    error_message: str | None
    poll_count: int

    @property
    def is_completed(self) -> bool:
        return self.status_name == "completed"

    @property
    def is_failed(self) -> bool:
        return self.status_name == "failed"


class RemoteCallClient:
    """Byte-oriented remote-call client backed by the Rust implementation."""

    def __init__(
        self,
        endpoint: str,
        *,
        metadata: Sequence[tuple[str, str]] = (),
        use_tls: bool | None = None,
        initial_poll_delay_ms: int = 50,
        max_poll_delay_ms: int = 5000,
        poll_backoff_multiplier: float = 1.7,
        max_message_size: int | None = None,
        auth_endpoint: str | None = None,
        exchange_token: str | None = None,
        requested_permissions: Sequence[int] | None = None,
        auth_rpc_timeout_ms: int | None = None,
        auth_retry_interval_ms: int | None = None,
    ) -> None:
        if _NativeRemoteCallClient is None:
            raise RemoteCallClientUnavailable(
                "chalkcompute remote calls require the native "
                "chalkcompute._remote_call_native extension"
            ) from _NATIVE_IMPORT_ERROR

        self._inner = _NativeRemoteCallClient(
            endpoint,
            list(metadata),
            use_tls,
            initial_poll_delay_ms,
            max_poll_delay_ms,
            poll_backoff_multiplier,
            max_message_size,
            auth_endpoint,
            exchange_token,
            list(requested_permissions) if requested_permissions is not None else None,
            auth_rpc_timeout_ms,
            auth_retry_interval_ms,
        )

    def call_ipc(self, name: str, input_ipc: bytes) -> list[bytes]:
        return list(self._inner.call_ipc(name, input_ipc))

    def enqueue(self, name: str, input_ipc: bytes) -> str:
        return str(self._inner.enqueue(name, input_ipc))

    def poll_next(
        self,
        call_id: str,
        cursor: str = "",
        poll_count: int = 0,
    ) -> RemoteCallPollResult:
        raw = self._inner.poll_next(call_id, cursor, poll_count)
        return RemoteCallPollResult(
            status=int(raw["status"]),
            status_name=str(raw["status_name"]),
            chunks=list(raw["chunks"]),
            cursor=str(raw["cursor"]),
            error_message=raw["error_message"],
            poll_count=int(raw["poll_count"]),
        )

    def get_result_chunks(self, call_id: str) -> list[bytes]:
        return list(self._inner.get_result_chunks(call_id))

    def purge_function(self, function_name: str) -> int:
        return int(self._inner.purge_function(function_name))

    def purge_all(self) -> dict[str, int]:
        return {str(k): int(v) for k, v in self._inner.purge_all().items()}
