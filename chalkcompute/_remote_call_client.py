"""Python wrapper around the native remote-call client."""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any, Sequence

logger = logging.getLogger(__name__)

_NativeRemoteCallClient: Any | None
_NATIVE_IMPORT_ERROR: ImportError | None
try:
    _remote_call_native = importlib.import_module("chalkcompute._remote_call_native")
except ImportError as exc:  # pragma: no cover - exercised in source checkouts
    _NativeRemoteCallClient = None
    _NATIVE_IMPORT_ERROR = exc
else:
    _NativeRemoteCallClient = getattr(_remote_call_native, "RemoteCallClient")
    _NATIVE_IMPORT_ERROR = None

_warned_per_call_metadata_unsupported = False


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
    """Byte-oriented remote-call client backed by the native Rust implementation."""

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

        auth_kwargs = (
            auth_endpoint,
            exchange_token,
            requested_permissions,
            auth_rpc_timeout_ms,
            auth_retry_interval_ms,
        )
        try:
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
                list(requested_permissions)
                if requested_permissions is not None
                else None,
                auth_rpc_timeout_ms,
                auth_retry_interval_ms,
            )
        except TypeError as exc:
            if any(value is not None for value in auth_kwargs):
                raise RemoteCallClientUnavailable(
                    "chalkcompute._remote_call_native is too old for service-auth "
                    "remote calls; rebuild/reinstall the local chalkcompute package"
                ) from exc
            self._inner = _NativeRemoteCallClient(
                endpoint,
                list(metadata),
                use_tls,
                initial_poll_delay_ms,
                max_poll_delay_ms,
                poll_backoff_multiplier,
                max_message_size,
            )
        self._supports_per_call_metadata: bool | None = None

    def _warn_per_call_metadata_unsupported(self) -> None:
        global _warned_per_call_metadata_unsupported
        if _warned_per_call_metadata_unsupported:
            return
        _warned_per_call_metadata_unsupported = True
        logger.warning(
            "Remote function tracing metadata was dropped because the installed "
            "chalkcompute._remote_call_native extension does not support per-call "
            "metadata. Rebuild or reinstall chalkcompute to enable nested remote-call tracing."
        )

    def call_ipc(
        self,
        name: str,
        input_ipc: bytes,
        metadata: Sequence[tuple[str, str]] = (),
    ) -> list[bytes]:
        if not metadata or self._supports_per_call_metadata is False:
            return list(self._inner.call_ipc(name, input_ipc))
        try:
            chunks = list(self._inner.call_ipc(name, input_ipc, list(metadata)))
            self._supports_per_call_metadata = True
            return chunks
        except TypeError:
            self._supports_per_call_metadata = False
            self._warn_per_call_metadata_unsupported()
            return list(self._inner.call_ipc(name, input_ipc))

    def enqueue(
        self,
        name: str,
        input_ipc: bytes,
        metadata: Sequence[tuple[str, str]] = (),
    ) -> str:
        if not metadata or self._supports_per_call_metadata is False:
            return str(self._inner.enqueue(name, input_ipc))
        try:
            call_id = str(self._inner.enqueue(name, input_ipc, list(metadata)))
            self._supports_per_call_metadata = True
            return call_id
        except TypeError:
            self._supports_per_call_metadata = False
            self._warn_per_call_metadata_unsupported()
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
