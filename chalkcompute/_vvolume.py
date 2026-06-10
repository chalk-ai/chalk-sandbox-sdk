"""Client utilities for Chalk versioned volumes."""

from __future__ import annotations

import builtins
import datetime
import importlib
import logging
import os
import uuid
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.volume.v2 import volume_pb2 as pb_v2
from chalkcompute._image import Image
from chalkcompute._tracing import traced
from chalkcompute._volume import (
    FileInfo,
    VolumeClosedError,
    VolumeError,
    VolumeInfo,
    VolumeMount,
    VolumeNotFoundError,
    _read_file_from_signed_uri,
)

logger = logging.getLogger(__name__)

_VERSIONED_INLINE_THRESHOLD_BYTES: int = 4 * 1024
_VERSIONED_CHUNK_SIZE_BYTES: int = 8 * 1024 * 1024
_VERSIONED_COMMIT_MAX_REBASE_RETRIES: int = 3
_VERSIONED_TRANSFER_MAX_WORKERS: int = 32
_VERSIONED_BATCH_MAX_FILES: int = 200

_remote_call_native: Any | None
_NativeVersionedVolumeClient: Any | None
_NativeVolumeRpcError: type[BaseException] | None
_NATIVE_IMPORT_ERROR: ImportError | None
try:
    _remote_call_native = importlib.import_module("chalkcompute._remote_call_native")
except ImportError as exc:  # pragma: no cover - exercised in source checkouts
    _remote_call_native = None
    _NativeVersionedVolumeClient = None
    _NativeVolumeRpcError = None
    _NATIVE_IMPORT_ERROR = exc
else:
    _NativeVersionedVolumeClient = getattr(
        _remote_call_native, "VersionedVolumeNativeClient", None
    )
    _NativeVolumeRpcError = getattr(_remote_call_native, "VolumeRpcError", None)
    _NATIVE_IMPORT_ERROR = None


@dataclass
class VersionInfo:
    """Metadata for an immutable versioned volume snapshot."""

    id: int
    sequence_number: int = 0
    parent_id: int | None = None
    created_at: datetime.datetime | None = None
    ref: str | None = None
    commit_id: str | None = None

    @property
    def version_id(self) -> int:
        """Alias for the protobuf ``version_id`` field name."""
        return self.id


@dataclass
class CommitStatus:
    """Result of a versioned volume commit attempt."""

    commit_id: str
    result: str
    version: VersionInfo | None = None
    latest_version: VersionInfo | None = None
    created_at: datetime.datetime | None = None


class VersionedVolume:
    """A handle to a versioned volume using the native v2 volume client."""

    def __init__(
        self,
        name: str,
        client: VersionedVolumeClient | None = None,
        *,
        version_id: int | None = None,
        ref: str = "main",
        create_if_missing: bool = True,
    ) -> None:
        self._client = client or VersionedVolumeClient.from_connect(ConnectClient())
        self._name = name
        self._version_id = version_id
        self._ref = ref
        self._info: VolumeInfo | None = None
        self._version: VersionInfo | None = None
        self._create_if_missing = create_if_missing and version_id is None
        self._closed = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def version_id(self) -> int | None:
        return self._version_id

    @property
    def info(self) -> VolumeInfo:
        self._raise_if_closed()
        if self._info is None:
            self.reload()
        assert self._info is not None
        return self._info

    @property
    def version(self) -> VersionInfo:
        self._raise_if_closed()
        if self._version is None:
            self.reload()
        if self._version is None:
            raise VolumeError(f"Versioned volume {self._name!r} has no version")
        return self._version

    def reload(self) -> VolumeInfo:
        self._info, self._version = self._fetch_info()
        return self._info

    def _fetch_info(self) -> tuple[VolumeInfo, VersionInfo | None]:
        self._raise_if_closed()
        try:
            raw = self._client._get_volume(
                self._name,
                version_id=self._version_id,
                ref_name=self._selector_ref(),
            )
        except VolumeNotFoundError:
            if not self._create_if_missing:
                raise
            raw = self._client._create_volume(self._name)
            return _dict_to_volume_info(raw["volume"]), None

        return (
            _dict_to_volume_info(raw["volume"]),
            _dict_to_version_info(raw["version"]) if raw.get("version") else None,
        )

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise VolumeClosedError(f"VersionedVolume {self._name!r} is closed")

    def _ensure_ready(self) -> None:
        self._raise_if_closed()
        if self._info is None:
            self._info, self._version = self._fetch_info()

    @traced("chalkcompute.versioned_volume.read_file")
    def read_file(self, path: str) -> bytes:
        self._ensure_ready()
        return self._client._download_bytes(
            self._name,
            path,
            version_id=self._read_version_id(),
        )

    @traced("chalkcompute.versioned_volume.put_file")
    def put_file(self, path: str, data: bytes | str) -> VersionedVolume:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._commit_files([(path, data)])
        return self

    def put_file_from_path(self, path: str, local_path: str | Path) -> VersionedVolume:
        self._ensure_mutable()
        self._ensure_ready()
        statuses = self._client._upload(
            self._name,
            [(path, Path(local_path))],
            batch_size=1,
        )
        self._update_version_from_statuses(statuses)
        return self

    def remove_file(self, path: str) -> VersionedVolume:
        self._ensure_mutable()
        self._ensure_ready()
        status = self._client._remove_files(
            self._name,
            [(path, False)],
            max_commit_retries=_VERSIONED_COMMIT_MAX_REBASE_RETRIES,
        )
        self._update_version_from_statuses([status])
        return self

    def listdir(self, prefix: str = "", *, recursive: bool = True) -> list[FileInfo]:
        self._ensure_ready()
        files = self._listdir_page_all(prefix)
        if not recursive:
            return files

        out = list(files)
        seen_dirs = {prefix.strip("/")}
        queue = [
            f.path
            for f in files
            if f.kind == "directory" and f.path.strip("/") not in seen_dirs
        ]
        while queue:
            directory = queue.pop(0).strip("/")
            seen_dirs.add(directory)
            children = self._listdir_page_all(directory)
            out.extend(children)
            queue.extend(
                f.path
                for f in children
                if f.kind == "directory" and f.path.strip("/") not in seen_dirs
            )
        return out

    def iterdir(self, prefix: str = "") -> Iterator[FileInfo]:
        yield from self.listdir(prefix)

    @contextmanager
    def batch_upload(
        self,
        *,
        max_files_per_commit: int = _VERSIONED_BATCH_MAX_FILES,
    ) -> Generator[_VersionedBatchUpload, None, None]:
        self._raise_if_closed()
        self._ensure_mutable()
        if max_files_per_commit <= 0:
            raise ValueError("max_files_per_commit must be positive")
        batch = _VersionedBatchUpload(
            self,
            max_files_per_commit=max_files_per_commit,
        )
        yield batch
        batch._flush()

    def versions(self, *, limit: int = 100) -> list[VersionInfo]:
        self._ensure_ready()
        versions: list[VersionInfo] = []
        cursor = ""
        while True:
            raw = self._client._list_volume_versions(
                self._name,
                limit=limit,
                cursor=cursor,
            )
            versions.extend(_dict_to_version_info(v) for v in raw["versions"])
            cursor = str(raw.get("next_cursor") or "")
            if not cursor:
                return versions

    def latest_version(self) -> VersionInfo:
        self._ensure_ready()
        raw = self._client._get_volume(self._name, ref_name=self._selector_ref())
        self._info = _dict_to_volume_info(raw["volume"])
        if not raw.get("version"):
            raise VolumeError(f"Versioned volume {self._name!r} has no latest version")
        self._version = _dict_to_version_info(raw["version"])
        return self._version

    def at_version(self, version_id: int) -> VersionedVolume:
        self._raise_if_closed()
        volume = VersionedVolume(
            self._name,
            client=self._client,
            version_id=version_id,
            ref=self._ref,
            create_if_missing=False,
        )
        volume._info = self._info
        return volume

    @traced("chalkcompute.versioned_volume.delete")
    def delete(self) -> None:
        self._ensure_mutable()
        self._ensure_ready()
        self._client.delete_volume(self._name)

    def download_to_file(self, remote_path: str, local_path: str | Path) -> None:
        self._ensure_ready()
        self._client._download_to_file(
            self._name,
            remote_path,
            Path(local_path),
            version_id=self._read_version_id(),
        )

    def download_to_directory(self, local_dir: str | Path) -> None:
        self._ensure_ready()
        self._client._download_to_directory(
            self._name,
            Path(local_dir),
            version_id=self._read_version_id(),
        )

    def _commit_files(self, files: list[tuple[str, bytes]]) -> VersionInfo:
        self._ensure_mutable()
        self._ensure_ready()
        statuses = self._client._upload(
            self._name,
            files,
            batch_size=max(1, len(files)),
        )
        return self._update_version_from_statuses(statuses)

    def _update_version_from_statuses(self, statuses: list[CommitStatus]) -> VersionInfo:
        committed = [status for status in statuses if status.version is not None]
        if not committed:
            raise VolumeError("Versioned volume commit returned no committed version")
        status = committed[-1]
        if status.result != "committed":
            raise VolumeError(
                f"Versioned volume commit {status.commit_id} returned {status.result}"
            )
        assert status.version is not None
        self._version = status.version
        return status.version

    def _listdir_page_all(self, path: str) -> list[FileInfo]:
        files: list[FileInfo] = []
        cursor = ""
        while True:
            raw = self._client._list_files(
                self._name,
                path=path,
                version_id=self._version_id,
                ref_name=self._selector_ref(),
                limit=1000,
                cursor=cursor,
            )
            files.extend(_dict_to_file_info(f) for f in raw["files"])
            if raw.get("version"):
                self._version = _dict_to_version_info(raw["version"])
            cursor = str(raw.get("next_cursor") or "")
            if not cursor:
                return files

    def _selector_ref(self) -> str | None:
        if self._version_id is not None:
            return None
        if self._ref and self._ref != "main":
            return self._ref
        return None

    def _read_version_id(self) -> int | None:
        if self._version_id is not None:
            return self._version_id
        if self._ref and self._ref != "main":
            return self.version.id
        return None

    def _ensure_mutable(self) -> None:
        self._raise_if_closed()
        if self._version_id is not None:
            raise VolumeError("VersionedVolume.at_version(...) handles are read-only")
        if self._ref and self._ref != "main":
            raise VolumeError("Versioned volume writes currently support only ref='main'")

    def close(self) -> None:
        if not self._closed:
            self._client.close()
        self._closed = True

    def __enter__(self) -> VersionedVolume:
        self._raise_if_closed()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        if self._version_id is not None:
            return (
                f"VersionedVolume(name={self._name!r}, version_id={self._version_id!r})"
            )
        return f"VersionedVolume(name={self._name!r}, ref={self._ref!r})"


class VersionedVolumeClient:
    """Client for the v2 versioned Volume service.

    The implementation is backed by ``chalkcompute._remote_call_native``'s
    PyO3 volume client, which delegates transfer and commit logic to Rust.
    """

    mount_type = "versioned_chalkfs"

    def __init__(
        self,
        endpoint: str = "http://localhost:50051",
        *,
        token: str = "",
        env_id: str | None = None,
        commit_author: str | None = None,
        extra_headers: builtins.list[tuple[str, str]] | None = None,
        chalk_server: str = "go-api",
        credentials: object | None = None,
        metadata: object | None = None,
        options: object | None = None,
    ) -> None:
        if _NativeVersionedVolumeClient is None:
            raise VolumeError(
                "versioned volumes require the native "
                "chalkcompute._remote_call_native extension"
            ) from _NATIVE_IMPORT_ERROR
        del options
        if metadata is not None:
            token = token or _metadata_value(metadata, "authorization").removeprefix(
                "Bearer "
            )
            env_id = env_id or _metadata_value(metadata, "x-chalk-env-id") or None
            chalk_server = (
                _metadata_value(metadata, "x-chalk-server") or chalk_server
            )
        endpoint = _native_endpoint(endpoint, use_tls=credentials is not None)
        self._endpoint = endpoint
        self._token = token
        self._env_id = env_id
        self._closed = False
        self._inner = _NativeVersionedVolumeClient(
            endpoint,
            token,
            env_id,
            commit_author,
            extra_headers or [],
            chalk_server,
        )

    @classmethod
    def from_connect(cls, connect: ConnectClient) -> VersionedVolumeClient:
        connect._ensure_auth()
        # No commit_author here on purpose. When the author is unset, the
        # native client derives a chalk:{environment}:agent:{agent_id} URN
        # from the credentials so dashboard commit history links back to
        # the authoring agent.
        return cls(
            connect._api_server or "",
            token=connect._access_token or "",
            env_id=connect._env_id or None,
            chalk_server="go-api",
        )

    def __enter__(self) -> VersionedVolumeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True

    @traced("chalkcompute.versioned_volume.create")
    def create(self, name: str) -> VersionedVolume:
        raw = self._create_volume(name)
        vol = VersionedVolume(name, client=self, create_if_missing=False)
        vol._info = _dict_to_volume_info(raw["volume"])
        return vol

    def from_name(self, name: str) -> VersionedVolume:
        return VersionedVolume(name, client=self, create_if_missing=False)

    def lookup(self, name: str) -> VersionedVolume:
        vol = VersionedVolume(name, client=self, create_if_missing=False)
        vol.reload()
        return vol

    def exists(self, name: str) -> bool:
        try:
            self._get_volume(name)
        except VolumeNotFoundError:
            return False
        return True

    @traced("chalkcompute.versioned_volume.upload_lazy_files")
    def upload_lazy_files(
        self, image: Image, *, volume_prefix: str = "files"
    ) -> builtins.list[VolumeMount]:
        try:
            dir_to_files = image.lazy_local_files_grouped_for_volume_mounts()
        except ValueError as e:
            raise VolumeError(str(e)) from e

        volumes: builtins.list[VolumeMount] = []
        for dest_dir, files in dir_to_files.items():
            vol = self.create(f"{volume_prefix}-{uuid.uuid4().hex[:12]}")
            with vol.batch_upload() as batch:
                for lf in files:
                    batch.put_file(os.path.relpath(lf.dest, dest_dir), lf.src)
            volumes.append(
                VolumeMount(name=vol.name, mount_path=dest_dir, type=self.mount_type)
            )
        return volumes

    def delete_volume(self, name: str) -> None:
        self._raise_if_closed()
        try:
            self._inner.delete_volume(name)
        except _native_error_types() as exc:
            _raise_volume_error(exc, name)

    def list(self, *, limit: int = 100) -> builtins.list[VolumeInfo]:
        volumes: list[VolumeInfo] = []
        cursor = ""
        while True:
            raw = self._list_volumes(limit=limit, cursor=cursor)
            volumes.extend(_dict_to_volume_info(v) for v in raw["volumes"])
            cursor = str(raw.get("next_cursor") or "")
            if not cursor:
                return volumes

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise VolumeClosedError("VersionedVolumeClient is closed")

    def _create_volume(self, name: str) -> dict[str, Any]:
        self._raise_if_closed()
        try:
            return dict(self._inner.create_volume(name))
        except _native_error_types() as exc:
            _raise_volume_error(exc, name)

    def _get_volume(
        self,
        name: str,
        *,
        version_id: int | None = None,
        ref_name: str | None = None,
    ) -> dict[str, Any]:
        self._raise_if_closed()
        try:
            return dict(self._inner.get_volume(name, version_id, ref_name))
        except _native_error_types() as exc:
            _raise_volume_error(exc, name)

    def _list_volumes(self, *, limit: int, cursor: str) -> dict[str, Any]:
        self._raise_if_closed()
        try:
            return dict(self._inner.list_volumes(limit, cursor))
        except _native_error_types() as exc:
            _raise_volume_error(exc, "")

    def _list_volume_versions(
        self,
        name: str,
        *,
        limit: int,
        cursor: str,
    ) -> dict[str, Any]:
        self._raise_if_closed()
        try:
            return dict(self._inner.list_volume_versions(name, limit, cursor))
        except _native_error_types() as exc:
            _raise_volume_error(exc, name)

    def _list_files(
        self,
        volume: str,
        *,
        path: str,
        version_id: int | None,
        ref_name: str | None,
        limit: int,
        cursor: str,
    ) -> dict[str, Any]:
        self._raise_if_closed()
        try:
            return dict(
                self._inner.list_files(
                    volume,
                    path,
                    version_id,
                    ref_name,
                    limit,
                    cursor,
                )
            )
        except _native_error_types() as exc:
            _raise_volume_error(exc, volume)

    def _upload(
        self,
        volume: str,
        files: Sequence[tuple[str, bytes | Path]],
        *,
        batch_size: int,
    ) -> builtins.list[CommitStatus]:
        self._raise_if_closed()
        config = {
            "chunk_size": _VERSIONED_CHUNK_SIZE_BYTES,
            "inline_threshold": _VERSIONED_INLINE_THRESHOLD_BYTES,
            "batch_size": batch_size,
            "max_commit_retries": _VERSIONED_COMMIT_MAX_REBASE_RETRIES,
        }
        try:
            raw_statuses = self._inner.upload(volume, builtins.list(files), config)
        except _native_error_types() as exc:
            _raise_volume_error(exc, volume)
        return [_dict_to_commit_status(dict(status)) for status in raw_statuses]

    def _remove_files(
        self,
        volume: str,
        paths: builtins.list[tuple[str, bool]],
        *,
        max_commit_retries: int,
    ) -> CommitStatus:
        self._raise_if_closed()
        try:
            raw = self._inner.remove_files(volume, paths, max_commit_retries)
        except _native_error_types() as exc:
            _raise_volume_error(exc, volume)
        return _dict_to_commit_status(dict(raw))

    def _download_bytes(
        self,
        volume: str,
        path: str,
        *,
        version_id: int | None,
    ) -> bytes:
        self._raise_if_closed()
        try:
            return bytes(self._inner.download_bytes(volume, path, version_id, None))
        except _native_error_types() as exc:
            _raise_volume_error(exc, volume)

    def _download_to_file(
        self,
        volume: str,
        remote_path: str,
        local_path: Path,
        *,
        version_id: int | None,
    ) -> None:
        self._raise_if_closed()
        try:
            self._inner.download_to_file(volume, remote_path, local_path, version_id, None)
        except _native_error_types() as exc:
            _raise_volume_error(exc, volume)

    def _download_to_directory(
        self,
        volume: str,
        local_dir: Path,
        *,
        version_id: int | None,
    ) -> None:
        self._raise_if_closed()
        try:
            self._inner.download_to_directory(volume, local_dir, version_id, None)
        except _native_error_types() as exc:
            _raise_volume_error(exc, volume)


def _dict_to_volume_info(info: dict[str, Any]) -> VolumeInfo:
    return VolumeInfo(
        name=str(info.get("name") or ""),
        created_at=_datetime_from_ms(info.get("created_at_ms")),
        volume_id=_optional_str(info.get("volume_id")),
        access_mode=_volume_access_mode_name(int(info.get("access_mode") or 0)),
        ref=_optional_str(info.get("ref")),
    )


def _dict_to_version_info(info: dict[str, Any]) -> VersionInfo:
    return VersionInfo(
        id=int(info.get("version_id") or 0),
        sequence_number=int(info.get("sequence_number") or 0),
        parent_id=(
            int(info["parent_id"]) if info.get("parent_id") is not None else None
        ),
        created_at=_datetime_from_ms(info.get("created_at_ms")),
        ref=_optional_str(info.get("ref")),
        commit_id=_optional_str(info.get("commit_id")),
    )


def _dict_to_file_info(info: dict[str, Any]) -> FileInfo:
    return FileInfo(
        path=str(info.get("path") or ""),
        size=int(info.get("size") or 0),
        updated_at=_datetime_from_ms(info.get("updated_at_ms")),
        kind=_file_kind_name(int(info.get("kind") or 0)),
        hash=_optional_str(info.get("hash")),
        mode=int(info["mode"]) if info.get("mode") is not None else None,
        uid=int(info.get("uid") or 0),
        gid=int(info.get("gid") or 0),
        nlink=int(info.get("nlink") or 0),
    )


def _dict_to_commit_status(status: dict[str, Any]) -> CommitStatus:
    return CommitStatus(
        commit_id=str(status.get("commit_id") or ""),
        result=str(status.get("result") or "unspecified"),
        version=(
            _dict_to_version_info(dict(status["version"]))
            if status.get("version") is not None
            else None
        ),
        latest_version=(
            _dict_to_version_info(dict(status["latest_version"]))
            if status.get("latest_version") is not None
            else None
        ),
        created_at=_datetime_from_ms(status.get("created_at_ms")),
    )


def _datetime_from_ms(value: Any) -> datetime.datetime | None:
    if value is None:
        return None
    return datetime.datetime.fromtimestamp(
        int(value) / 1000,
        tz=datetime.timezone.utc,
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _metadata_value(metadata: Any, key: str) -> str:
    for item in tuple(metadata or ()):
        if len(item) != 2:
            continue
        k, v = item
        if str(k).lower() == key:
            return str(v)
    return ""


def _native_endpoint(endpoint: str, *, use_tls: bool) -> str:
    if endpoint.startswith(("http://", "https://")):
        return endpoint.rstrip("/")
    scheme = "https" if use_tls else "http"
    return f"{scheme}://{endpoint.rstrip('/')}"


def _native_error_types() -> tuple[type[BaseException], ...]:
    if _NativeVolumeRpcError is None:
        return (RuntimeError,)
    return (_NativeVolumeRpcError,)


def _native_error_code(exc: BaseException) -> str:
    if len(exc.args) >= 1:
        return str(exc.args[0])
    return ""


def _native_error_message(exc: BaseException) -> str:
    if len(exc.args) >= 2:
        return str(exc.args[1])
    return str(exc)


def _raise_volume_error(exc: BaseException, volume_name: str) -> NoReturn:
    code = _native_error_code(exc)
    message = _native_error_message(exc)
    if code == "not_found":
        raise VolumeNotFoundError(f"Volume {volume_name!r} not found") from exc
    raise VolumeError(message) from exc


def _volume_access_mode_name(value: int) -> str | None:
    return {
        1: "read_only",
        2: "read_write",
    }.get(value)


def _file_kind_name(value: int) -> str:
    return {
        1: "file",
        2: "directory",
        3: "symlink",
    }.get(value, "unspecified")


def _read_versioned_file_response(resp: pb_v2.GetFileResponse) -> bytes:
    content_type = resp.WhichOneof("content")
    if content_type == "data":
        return resp.data
    if content_type == "chunked":
        chunks = sorted(resp.chunked.chunks, key=lambda c: c.offset)
        if not chunks:
            return b""
        return b"".join(_read_file_from_signed_uri(c.signed_download_uri) for c in chunks)
    if content_type == "packed":
        pack = resp.packed.pack
        data = _read_file_from_signed_uri(pack.signed_download_uri)
        return data[pack.offset : pack.offset + pack.size]
    raise VolumeError(f"Unsupported versioned file response type: {content_type!r}")


class _VersionedBatchUpload:
    def __init__(
        self,
        volume: VersionedVolume,
        *,
        max_files_per_commit: int,
    ) -> None:
        self._volume = volume
        self._max_files_per_commit = max_files_per_commit
        self._pending: list[tuple[str, bytes | Path]] = []

    def put(self, path: str, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._pending.append((path, data))
        if len(self._pending) >= self._max_files_per_commit:
            self._flush()

    def put_file(self, path: str, local_path: str | Path) -> None:
        self._pending.append((path, Path(local_path)))
        if len(self._pending) >= self._max_files_per_commit:
            self._flush()

    def _flush(self) -> None:
        if not self._pending:
            return
        files = self._pending
        self._pending = []
        statuses = self._volume._client._upload(
            self._volume.name,
            files,
            batch_size=len(files),
        )
        self._volume._update_version_from_statuses(statuses)
