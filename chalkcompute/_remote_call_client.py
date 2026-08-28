"""Python wrapper around the native remote-call client."""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).resolve().parent


def _find_native_extension_path(
    search_paths: Sequence[str] | None = None,
) -> Path | None:
    paths = sys.path if search_paths is None else search_paths
    for raw_path in paths:
        try:
            package_dir = (Path(raw_path) if raw_path else Path.cwd()).resolve() / (
                "chalkcompute"
            )
        except OSError:
            continue
        if package_dir == _PACKAGE_DIR:
            continue
        for suffix in importlib.machinery.EXTENSION_SUFFIXES:
            candidate = package_dir / f"_native{suffix}"
            if candidate.is_file():
                return candidate
    return None


def _load_native_extension_from_path(native_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(
        "chalkcompute._native",
        native_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load native extension from {native_path}")

    module = importlib.util.module_from_spec(spec)
    previous_module = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if previous_module is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous_module
        raise
    return module


def _import_native() -> Any:
    try:
        return importlib.import_module("chalkcompute._native")
    except ImportError:
        native_path = _find_native_extension_path()
        if native_path is None:
            raise
        logger.info(
            "Falling back to native extension from %s",
            native_path,
        )
        try:
            return _load_native_extension_from_path(native_path)
        except Exception as fallback_exc:
            raise ImportError(
                f"failed to load chalkcompute._native from {native_path}"
            ) from fallback_exc


_NativeRemoteCallClient: Any | None
_NATIVE_IMPORT_ERROR: ImportError | None
_native: Any | None = None
try:
    _native = _import_native()
except ImportError as exc:  # pragma: no cover - exercised in source checkouts
    _NativeRemoteCallClient = None
    _NATIVE_IMPORT_ERROR = exc
else:
    _NativeRemoteCallClient = getattr(_native, "RemoteCallClient")
    _NATIVE_IMPORT_ERROR = None

_warned_per_call_metadata_unsupported = False


class RemoteCallClientUnavailable(RuntimeError):
    """Raised when the native remote-call extension is not installed."""


@dataclass(frozen=True)
class ServiceAuthContext:
    auth_endpoint: str
    exchange_token: str
    environment_id: str
    requested_permissions: tuple[int, ...]
    grpc_engine: tuple[str, bool] | None


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
        return self.status_name.lower() == "completed"

    @property
    def is_failed(self) -> bool:
        return self.status_name.lower() == "failed"


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
        call_timeout_ms: int | None = None,
    ) -> None:
        if _NativeRemoteCallClient is None:
            raise RemoteCallClientUnavailable(
                "chalkcompute remote calls require the native "
                "chalkcompute._native extension"
            ) from _NATIVE_IMPORT_ERROR

        auth_kwargs = (
            auth_endpoint,
            exchange_token,
            requested_permissions,
            auth_rpc_timeout_ms,
            auth_retry_interval_ms,
        )
        start = time.monotonic()
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
                call_timeout_ms,
            )
        except TypeError as exc:
            if any(value is not None for value in auth_kwargs):
                raise RemoteCallClientUnavailable(
                    "chalkcompute._native is too old for service-auth "
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
        self._closed = False
        self._supports_per_call_metadata: bool = (
            self._detect_per_call_metadata_support()
        )
        logger.debug(
            "RemoteCallClient connected endpoint=%s use_tls=%s elapsed_ms=%.1f",
            endpoint,
            use_tls,
            (time.monotonic() - start) * 1000,
        )

    def _inner_or_raise(self) -> Any:
        if self._closed:
            raise RuntimeError("RemoteCallClient is closed")
        return self._inner

    def close(self) -> None:
        """Release the underlying native client, if one is still attached."""
        if self._closed:
            return
        inner = self._inner
        close = getattr(inner, "close", None)
        try:
            if callable(close):
                close()
        finally:
            self._inner = None
            self._closed = True

    def _detect_per_call_metadata_support(self) -> bool:
        """Return True if the native extension's call_ipc accepts a metadata arg."""
        global _warned_per_call_metadata_unsupported
        import inspect

        try:
            sig = inspect.signature(self._inner_or_raise().call_ipc)
            supported = len(sig.parameters) >= 3
        except (ValueError, TypeError):
            # Can't introspect — assume capable; a TypeError on the real call
            # will surface naturally.
            supported = True

        if not supported and not _warned_per_call_metadata_unsupported:
            _warned_per_call_metadata_unsupported = True
            logger.warning(
                "chalkcompute._native does not support per-call "
                "metadata. Rebuild or reinstall chalkcompute to enable "
                "nested remote-call tracing."
            )
        return supported

    def call_ipc(
        self,
        name: str,
        input_ipc: bytes,
        metadata: Sequence[tuple[str, str]] = (),
    ) -> list[bytes]:
        start = time.monotonic()
        if metadata and self._supports_per_call_metadata:
            chunks = list(
                self._inner_or_raise().call_ipc(name, input_ipc, list(metadata))
            )
        else:
            chunks = list(self._inner_or_raise().call_ipc(name, input_ipc))
        logger.debug(
            "RemoteCallClient call_ipc function=%s request_bytes=%d chunks=%d elapsed_ms=%.1f",
            name,
            len(input_ipc),
            len(chunks),
            (time.monotonic() - start) * 1000,
        )
        return chunks

    def enqueue(
        self,
        name: str,
        input_ipc: bytes,
        metadata: Sequence[tuple[str, str]] = (),
    ) -> str:
        start = time.monotonic()
        if metadata and self._supports_per_call_metadata:
            call_id = str(
                self._inner_or_raise().enqueue(name, input_ipc, list(metadata))
            )
        else:
            call_id = str(self._inner_or_raise().enqueue(name, input_ipc))
        logger.debug(
            "RemoteCallClient enqueue function=%s request_bytes=%d elapsed_ms=%.1f",
            name,
            len(input_ipc),
            (time.monotonic() - start) * 1000,
        )
        return call_id

    def poll_next(
        self,
        call_id: str,
        cursor: str = "",
        poll_count: int = 0,
    ) -> RemoteCallPollResult:
        start = time.monotonic()
        raw = self._inner_or_raise().poll_next(call_id, cursor, poll_count)
        result = RemoteCallPollResult(
            status=int(raw["status"]),
            status_name=str(raw["status_name"]),
            chunks=list(raw["chunks"]),
            cursor=str(raw["cursor"]),
            error_message=raw["error_message"],
            poll_count=int(raw["poll_count"]),
        )
        logger.debug(
            "RemoteCallClient poll_next call_id=%s status=%s chunks=%d elapsed_ms=%.1f",
            call_id,
            result.status_name,
            len(result.chunks),
            (time.monotonic() - start) * 1000,
        )
        return result

    def get_result_chunks(self, call_id: str) -> list[bytes]:
        return list(self._inner_or_raise().get_result_chunks(call_id))

    def purge_function(self, function_name: str) -> int:
        return int(self._inner_or_raise().purge_function(function_name))

    def purge_all(self) -> dict[str, int]:
        return {str(k): int(v) for k, v in self._inner_or_raise().purge_all().items()}


def resolve_service_auth_context() -> ServiceAuthContext | None:
    if _NativeRemoteCallClient is None:
        raise RemoteCallClientUnavailable(
            "chalkcompute service-auth environment resolution requires the "
            "native chalkcompute._native extension"
        ) from _NATIVE_IMPORT_ERROR

    resolver = getattr(_native, "resolve_service_auth_context", None)
    if resolver is None:
        raise RemoteCallClientUnavailable(
            "chalkcompute._native is too old for service-auth "
            "environment resolution; rebuild/reinstall the local chalkcompute package"
        )

    raw = resolver()
    if raw is None:
        return None

    raw_grpc_engine = raw["grpc_engine"]
    grpc_engine = None
    if raw_grpc_engine is not None:
        grpc_engine = (
            str(raw_grpc_engine["target"]),
            bool(raw_grpc_engine["use_tls"]),
        )

    return ServiceAuthContext(
        auth_endpoint=str(raw["auth_endpoint"]),
        exchange_token=str(raw["exchange_token"]),
        environment_id=str(raw["environment_id"]),
        requested_permissions=tuple(
            int(value) for value in raw["requested_permissions"]
        ),
        grpc_engine=grpc_engine,
    )
