from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from chalkcompute._function_error import FunctionUnavailableError

class RemoteCallClient:
    def __init__(
        self,
        endpoint: str,
        metadata: Sequence[tuple[str, str]] | None = None,
        use_tls: bool | None = None,
        initial_poll_delay_ms: int = 50,
        max_poll_delay_ms: int = 1000,
        poll_backoff_multiplier: float = 1.7,
        max_message_size: int | None = None,
        auth_endpoint: str | None = None,
        exchange_token: str | None = None,
        requested_permissions: Sequence[int] | None = None,
        auth_rpc_timeout_ms: int | None = None,
        auth_retry_interval_ms: int | None = None,
        api_mode: bool = False,
    ) -> None: ...
    def call_ipc(
        self,
        name: str,
        input_ipc: bytes,
        metadata: Sequence[tuple[str, str]] | None = None,
        retry_policy: dict[str, object] | None = None,
        timeout: float | None = None,
    ) -> list[bytes]: ...
    def call_external_ipc(
        self,
        name: str,
        version: int | None,
        input_ipc: bytes,
        metadata: Sequence[tuple[str, str]] | None = None,
        retry_policy: dict[str, object] | None = None,
        timeout: float | None = None,
    ) -> list[bytes]: ...
    def enqueue(
        self,
        name: str,
        input_ipc: bytes,
        metadata: Sequence[tuple[str, str]] | None = None,
        max_retries: int | None = None,
    ) -> str: ...
    def poll_next(
        self,
        call_id: str,
        cursor: str | None = None,
        poll_count: int = 0,
    ) -> dict[str, Any]: ...
    def get_result_chunks(self, call_id: str) -> list[bytes]: ...
    def purge_function(self, function_name: str) -> int: ...
    def purge_all(self) -> dict[str, int]: ...

class RemoteCallUnavailableError(FunctionUnavailableError): ...
class RemoteCallTransportError(FunctionUnavailableError): ...
class VolumeRpcError(RuntimeError): ...

class StagedFiles:
    def __init__(self) -> None: ...
    def extend(self, other: StagedFiles) -> None: ...
    def __len__(self) -> int: ...

class PreparedFiles:
    @property
    def commit_id(self) -> str: ...
    @property
    def ref_name(self) -> str: ...
    def __len__(self) -> int: ...

class VolumeNativeClient:
    def __init__(
        self,
        endpoint: str,
        token: str,
        env_id: str | None = None,
        commit_author: str | None = None,
        extra_headers: Sequence[tuple[str, str]] | None = None,
        chalk_server: str | None = None,
        token_refresher: Any | None = None,
    ) -> None: ...
    def upload(
        self,
        volume: str,
        source: Any,
        config: dict[str, Any] | None = None,
        ref_name: str | None = None,
    ) -> list[dict[str, Any]]: ...
    def stage(
        self,
        volume: str,
        source: Any,
        config: dict[str, Any] | None = None,
    ) -> StagedFiles: ...
    def prepare_files(
        self,
        source: Any,
        ref_name: str | None = None,
    ) -> PreparedFiles: ...
    def stage_prepared_files(
        self,
        volume: str,
        prepared: PreparedFiles,
        config: dict[str, Any] | None = None,
    ) -> StagedFiles: ...
    def resolve_prepared_files(
        self,
        volume: str,
        volume_kind: str,
        prepared: PreparedFiles,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
    def stage_remote(
        self,
        volume: str,
        source: Sequence[tuple[str, str, int] | tuple[str, str, int, dict[str, Any]]],
        config: dict[str, Any] | None = None,
    ) -> StagedFiles: ...
    def staged_intent(
        self,
        volume: str,
        staged: StagedFiles,
        ref_name: str | None = None,
        removes: list[tuple[str, bool]] | None = None,
        volume_type: str | None = None,
    ) -> bytes: ...
    def commit_staged(
        self,
        volume: str,
        staged: StagedFiles,
        commit_id: str | None = None,
        max_commit_retries: int = 3,
        ref_name: str | None = None,
    ) -> dict[str, Any]: ...
    def download_bytes(
        self,
        volume: str,
        path: str,
        version_id: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> bytes: ...
    def download_to_file(
        self,
        volume: str,
        remote_path: str,
        local_path: Any,
        version_id: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> None: ...
    def download_to_directory(
        self,
        volume: str,
        local_dir: Any,
        version_id: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> None: ...
    def remove_files(
        self,
        volume: str,
        paths: Sequence[tuple[str, bool]],
        max_commit_retries: int = 3,
        ref_name: str | None = None,
    ) -> dict[str, Any]: ...
    def create_volume(
        self, name: str, volume_type: str | None = None
    ) -> dict[str, Any]: ...
    def get_volume(
        self,
        name: str,
        version_id: int | None = None,
        ref_name: str | None = None,
    ) -> dict[str, Any]: ...
    def list_volumes(
        self,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...
    def delete_volume(self, name: str) -> None: ...
    def list_volume_versions(
        self,
        name: str,
        limit: int | None = None,
        cursor: str | None = None,
        ref_name: str | None = None,
    ) -> dict[str, Any]: ...
    def list_files(
        self,
        volume: str,
        path: str | None = None,
        version_id: int | None = None,
        ref_name: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...
    def get_commit_status(self, volume: str, commit_id: str) -> dict[str, Any]: ...
    def create_ref(
        self, volume: str, name: str, from_version_id: int
    ) -> dict[str, Any]: ...
    def list_refs(self, volume: str) -> list[dict[str, Any]]: ...
    def delete_ref(self, volume: str, name: str) -> None: ...

def configure_volume_client_logging(
    level: str = "info",
    logger_name: str = "chalk.volume.client",
) -> bool: ...
def resolve_service_auth_context() -> dict[str, Any] | None: ...
def notebook_source_dependencies(sources: list[str]) -> list[int]: ...
