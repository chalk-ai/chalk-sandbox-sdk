from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class RemoteCallClient:
    def __init__(
        self,
        endpoint: str,
        metadata: Sequence[tuple[str, str]] | None = None,
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
    ) -> None: ...

    def call_ipc(self, name: str, input_ipc: bytes) -> list[bytes]: ...

    def enqueue(self, name: str, input_ipc: bytes) -> str: ...

    def poll_next(
        self,
        call_id: str,
        cursor: str | None = None,
        poll_count: int = 0,
    ) -> dict[str, Any]: ...

    def get_result_chunks(self, call_id: str) -> list[bytes]: ...

    def purge_function(self, function_name: str) -> int: ...

    def purge_all(self) -> dict[str, int]: ...
