"""Client utilities for Chalk volumes.

Examples
--------
>>> from chalkcompute import Volume
>>> vol = Volume("my-data")
>>> vol.put_file("models/latest.bin", model_bytes)
>>> data = vol.read_file("models/latest.bin")
>>> for f in vol.listdir("data/"):
...     print(f"{f.path}  {f.size} bytes")
"""

from __future__ import annotations

import builtins
import datetime
import importlib
import logging
import os
import uuid
from collections.abc import Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import httpx
from google.protobuf.json_format import MessageToDict

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.volume.v2 import volume_pb2 as pb_v2
from chalkcompute._image import Image
from chalkcompute._tracing import traced

logger = logging.getLogger(__name__)

#: Mount type stamped into container/sandbox specs for every Chalk volume.
MOUNT_TYPE = "versioned_chalkfs"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VolumeError(Exception):
    """Base exception for volume errors."""


class VolumeClosedError(VolumeError):
    """Raised when an operation is attempted on a closed volume."""


class VolumeNotFoundError(VolumeError):
    """Raised when a volume is not found."""


class VolumeFileNotFoundError(VolumeError):
    """Raised when a file within a volume is not found."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class VolumeInfo:
    """Metadata about a volume.

    Attributes
    ----------
    name
        Name of the volume.
    created_at
        Timestamp when the volume was created.
    """

    name: str
    created_at: datetime.datetime | None = None
    volume_id: str | None = None
    access_mode: str | None = None
    ref: str | None = None


@dataclass(frozen=True)
class VolumeMount:
    """A volume to mount into a container, sandbox, or scaling group.

    Attributes
    ----------
    name
        Volume name.
    mount_path
        Absolute path inside the container where the volume is mounted.
    """

    name: str
    mount_path: str

    def to_spec_dict(self) -> dict[str, str]:
        """Render this mount as a container-spec ``volumes`` entry."""
        return {
            "name": self.name,
            "mountPath": self.mount_path,
            "type": MOUNT_TYPE,
        }


@dataclass
class FileInfo:
    """Metadata about a file inside a volume.

    Attributes
    ----------
    path
        File path within the volume.
    size
        File size in bytes.
    updated_at
        Timestamp when the file was last updated.
    """

    path: str
    size: int
    updated_at: datetime.datetime | None = None
    kind: str | None = None
    hash: str | None = None
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    nlink: int | None = None


def _read_file_from_signed_uri(signed_download_uri: str) -> bytes:
    """Download a file from object storage via a signed URL.

    Parameters
    ----------
    signed_download_uri
        Signed URL returned by the control plane.

    Returns
    -------
    bytes
        Raw file contents.

    Raises
    ------
    VolumeError
        If the download times out or the HTTP request fails.
    """
    try:
        resp = httpx.get(signed_download_uri, timeout=300)
        resp.raise_for_status()
        return resp.content
    except httpx.TimeoutException as e:
        raise VolumeError(f"Timed out downloading signed file URL: {e}") from e
    except httpx.HTTPStatusError as e:
        raise VolumeError(
            f"Signed file download failed with status {e.response.status_code}"
        ) from e
    except httpx.HTTPError as e:
        raise VolumeError(f"Signed file download failed: {e}") from e


_INLINE_THRESHOLD_BYTES: int = 4 * 1024
_CHUNK_SIZE_BYTES: int = 8 * 1024 * 1024
_MAX_PACK_BYTES: int = 64 * 1024 * 1024
_COMMIT_MAX_REBASE_RETRIES: int = 3
_TRANSFER_MAX_WORKERS: int = 32
_FILE_CONCURRENCY: int = _TRANSFER_MAX_WORKERS
_CHUNK_CONCURRENCY: int = _TRANSFER_MAX_WORKERS
_CHUNK_MAX_RETRIES: int = 3
_RATE_LIMIT_MAX_RETRIES: int = 8
_BATCH_MAX_FILES: int = 200

_native: Any | None
_NativeVolumeClient: Any | None
_NativeVolumeRpcError: type[BaseException] | None
_NATIVE_IMPORT_ERROR: ImportError | None
try:
    _native = importlib.import_module("chalkcompute._native")
except ImportError as exc:
    _native = None
    _NativeVolumeClient = None
    _NativeVolumeRpcError = None
    _NATIVE_IMPORT_ERROR = exc
else:
    # Symbol name is fixed by the Rust `chalk_volume_client` crate; it keeps the
    # "Versioned" prefix even though the Python-side name has dropped it.
    _NativeVolumeClient = getattr(_native, "VersionedVolumeNativeClient", None)
    _NativeVolumeRpcError = getattr(_native, "VolumeRpcError", None)
    _NATIVE_IMPORT_ERROR = None


@dataclass
class VersionInfo:
    """Metadata for an immutable volume snapshot.

    Attributes
    ----------
    id
        Stable identifier of this version.
    sequence_number
        Monotonic position of this version on its ref.
    parent_id
        Version this one was committed against, if any.
    created_at
        Server-side commit timestamp.
    ref
        Ref this version was committed on, if known.
    commit_id
        Client-supplied commit identifier that produced this version.
    """

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
    """Result of a volume commit attempt.

    Attributes
    ----------
    commit_id
        Client-generated id used to look up commit progress.
    result
        Commit outcome (``"committed"``, ``"pending"``,
        ``"rebase_required"``, ``"aborted"``, or ``"unspecified"``).
    version
        The new version on success, or ``None`` otherwise.
    latest_version
        The newest known version, useful when a rebase is required.
    created_at
        Server-side timestamp for the commit attempt.
    """

    commit_id: str
    result: str
    version: VersionInfo | None = None
    latest_version: VersionInfo | None = None
    created_at: datetime.datetime | None = None


class Volume:
    """A handle to a volume using the v2 volume service.

    Volumes expose immutable snapshots behind mutable refs. The
    Python API supports direct file writes, reads, listing, and version
    lookup. Each write produces a new immutable version; the ``main`` ref
    (or a caller-provided ref) advances to point at the latest committed
    version.

    Examples
    --------
    Write a file, advance the ref, and read the result back:

    >>> from chalkcompute import Volume
    >>> with Volume("models") as vol:
    ...     vol.put_file("config.yaml", b"lr: 0.01\\n")
    ...     vol.read_file("config.yaml")
    b'lr: 0.01\\n'

    Pin a handle to a previous version and read from it:

    >>> previous = vol.at_version(vol.versions()[-2].id)
    >>> _ = previous.read_file("config.yaml")
    """

    def __init__(
        self,
        name: str,
        client: VolumeClient | None = None,
        *,
        version_id: int | None = None,
        ref: str = "main",
        create_if_missing: bool = True,
    ) -> None:
        """Initialize a volume handle.

        Parameters
        ----------
        name
            Volume name.
        client
            Optional :class:`VolumeClient`. If ``None``, one is
            constructed from a default :class:`ConnectClient`.
        version_id
            If set, pin this handle to a specific immutable version. Such
            handles are read-only; mutating operations raise
            :class:`VolumeError`.
        ref
            Mutable ref to read/write through (defaults to ``"main"``).
        create_if_missing
            If ``True`` (the default), the volume is created on first
            access if it does not already exist. Ignored when
            ``version_id`` is set.
        """
        self._client = client or VolumeClient.from_connect(ConnectClient())
        self._name = name
        self._version_id = version_id
        self._ref = ref
        self._info: VolumeInfo | None = None
        self._version: VersionInfo | None = None
        self._create_if_missing = create_if_missing and version_id is None
        self._closed = False

    @property
    def name(self) -> str:
        """Volume name."""
        return self._name

    @property
    def version_id(self) -> int | None:
        """Pinned version id, or ``None`` for a ref-tracking handle."""
        return self._version_id

    @property
    def info(self) -> VolumeInfo:
        """Cached volume metadata, fetched on first access.

        Returns
        -------
        VolumeInfo
            Metadata for this volume.
        """
        self._raise_if_closed()
        if self._info is None:
            self.reload()
        assert self._info is not None
        return self._info

    @property
    def version(self) -> VersionInfo:
        """Cached version metadata for the current ref or pinned version.

        Returns
        -------
        VersionInfo
            The version this handle currently points at.

        Raises
        ------
        VolumeError
            If the volume has no version yet (newly created, no commits).
        """
        self._raise_if_closed()
        if self._version is None:
            self.reload()
        if self._version is None:
            raise VolumeError(f"Volume {self._name!r} has no version")
        return self._version

    def reload(self) -> VolumeInfo:
        """Refresh volume and version metadata from the server.

        Returns
        -------
        VolumeInfo
            Fresh metadata for this volume.
        """
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
            raise VolumeClosedError(f"Volume {self._name!r} is closed")

    def _ensure_ready(self) -> None:
        self._raise_if_closed()
        if self._info is None:
            self._info, self._version = self._fetch_info()

    @traced("chalkcompute.volume.read_file")
    def read_file(self, path: str) -> bytes:
        """Read a file's contents at the current version.

        Parameters
        ----------
        path
            File path within the volume.

        Returns
        -------
        bytes
            The raw file bytes.
        """
        self._ensure_ready()
        return self._client._download_bytes(
            self._name,
            path,
            version_id=self._read_version_id(),
        )

    @traced("chalkcompute.volume.put_file")
    def put_file(self, path: str, data: bytes | str) -> Volume:
        """Write a file, committing a new version on the current ref.

        Small payloads are committed inline; large payloads are chunked
        and uploaded to object storage before the commit completes.

        Parameters
        ----------
        path
            Destination path within the volume.
        data
            File contents (``bytes`` or ``str``).

        Returns
        -------
        Volume
            ``self``, to allow chaining.
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._commit_files([(path, data)])
        return self

    def put_file_from_path(self, path: str, local_path: str | Path) -> Volume:
        """Upload a local file into the volume.

        Parameters
        ----------
        path
            Destination path within the volume.
        local_path
            Local file to read and upload.

        Returns
        -------
        Volume
            ``self``, to allow chaining.
        """
        self._ensure_mutable()
        self._ensure_ready()
        statuses = self._client._upload(
            self._name,
            [(path, Path(local_path))],
            batch_size=1,
        )
        self._update_version_from_statuses(statuses)
        return self

    def remove_file(self, path: str) -> Volume:
        """Remove a file, committing a new version on the current ref.

        Parameters
        ----------
        path
            File path within the volume.

        Returns
        -------
        Volume
            ``self``, to allow chaining.
        """
        self._ensure_mutable()
        self._ensure_ready()
        status = self._client._remove_files(
            self._name,
            [(path, False)],
            max_commit_retries=_COMMIT_MAX_REBASE_RETRIES,
        )
        self._update_version_from_statuses([status])
        return self

    def listdir(self, prefix: str = "", *, recursive: bool = True) -> list[FileInfo]:
        """List files at the current version.

        Parameters
        ----------
        prefix
            Only return entries under this directory prefix.
        recursive
            If ``True`` (the default), descend into subdirectories.

        Returns
        -------
        list of FileInfo
            One entry per matching file or directory.
        """
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
        """Iterate over files at the current version.

        Parameters
        ----------
        prefix
            Only return entries under this directory prefix.

        Yields
        ------
        FileInfo
            One entry per matching file or directory.
        """
        yield from self.listdir(prefix)

    @contextmanager
    def batch_upload(
        self,
        *,
        max_files_per_commit: int = _BATCH_MAX_FILES,
    ) -> Generator[_BatchUpload, None, None]:
        """Context manager that commits staged writes as one version.

        Files staged within the block are bundled into a single commit
        when the block exits (or when ``max_files_per_commit`` is reached).

        Parameters
        ----------
        max_files_per_commit
            Maximum files per commit before flushing automatically.

        Yields
        ------
        _BatchUpload
            A batch handle with ``put`` / ``put_file`` methods.

        Examples
        --------
        >>> with vol.batch_upload() as batch:
        ...     batch.put("a.txt", b"hello")
        ...     batch.put("b.txt", b"world")
        """
        self._raise_if_closed()
        self._ensure_mutable()
        if max_files_per_commit <= 0:
            raise ValueError("max_files_per_commit must be positive")
        self._ensure_ready()
        batch = _BatchUpload(
            self,
            max_files_per_commit=max_files_per_commit,
        )
        yield batch
        batch._flush()

    def versions(self, *, limit: int = 100) -> list[VersionInfo]:
        """List all versions of this volume.

        Parameters
        ----------
        limit
            Page size used when traversing the server-side cursor.

        Returns
        -------
        list of VersionInfo
            One entry per immutable version, ordered by the server.
        """
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
        """Fetch the latest committed version on this handle's ref.

        Returns
        -------
        VersionInfo
            The version currently pointed at by the ref.

        Raises
        ------
        VolumeError
            If the volume has no committed versions yet.
        """
        self._ensure_ready()
        raw = self._client._get_volume(self._name, ref_name=self._selector_ref())
        self._info = _dict_to_volume_info(raw["volume"])
        if not raw.get("version"):
            raise VolumeError(f"Volume {self._name!r} has no latest version")
        self._version = _dict_to_version_info(raw["version"])
        return self._version

    def at_version(self, version_id: int) -> Volume:
        """Return a read-only handle pinned to a specific version.

        Mutating operations on the returned handle raise
        :class:`VolumeError`.

        Parameters
        ----------
        version_id
            The immutable version to pin to.

        Returns
        -------
        Volume
            A new handle pointing at ``version_id``.
        """
        self._raise_if_closed()
        volume = Volume(
            self._name,
            client=self._client,
            version_id=version_id,
            ref=self._ref,
            create_if_missing=False,
        )
        volume._info = self._info
        return volume

    @traced("chalkcompute.volume.delete")
    def delete(self) -> None:
        """Delete this volume and all of its versions.

        Raises
        ------
        VolumeError
            If called on a version-pinned (read-only) handle.
        """
        self._ensure_mutable()
        self._ensure_ready()
        self._client.delete_volume(self._name)

    def download_to_file(self, remote_path: str, local_path: str | Path) -> None:
        """Download a file from the current version to a local path.

        Parameters
        ----------
        remote_path
            File path within the volume.
        local_path
            Local destination path to write.
        """
        self._ensure_ready()
        self._client._download_to_file(
            self._name,
            remote_path,
            Path(local_path),
            version_id=self._read_version_id(),
        )

    def download_to_directory(self, local_dir: str | Path) -> None:
        """Download the current version into a local directory.

        Parameters
        ----------
        local_dir
            Local destination directory.
        """
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

    def _update_version_from_statuses(
        self, statuses: list[CommitStatus]
    ) -> VersionInfo:
        committed = [status for status in statuses if status.version is not None]
        if not committed:
            raise VolumeError("Volume commit returned no committed version")
        status = committed[-1]
        if status.result != "committed":
            raise VolumeError(
                f"Volume commit {status.commit_id} returned {status.result}"
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
            raise VolumeError("Volume.at_version(...) handles are read-only")
        if self._ref and self._ref != "main":
            raise VolumeError("Volume writes currently support only ref='main'")

    def close(self) -> None:
        """Close the underlying client used by this Volume.

        Safe to call multiple times.
        """
        if not self._closed:
            self._client.close()
        self._closed = True

    def __enter__(self) -> Volume:
        """Enter a context that closes the volume on exit."""
        self._raise_if_closed()
        return self

    def __exit__(self, *_args: object) -> None:
        """Close the volume on context exit."""
        self.close()

    def __repr__(self) -> str:
        if self._version_id is not None:
            return f"Volume(name={self._name!r}, version_id={self._version_id!r})"
        return f"Volume(name={self._name!r}, ref={self._ref!r})"


class VolumeClient:
    """Client for the Chalk Volume service."""

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
        """Initialize the native volume client.

        Parameters
        ----------
        endpoint
            Volume service endpoint. Values without a scheme are converted
            to ``http://`` or ``https://`` depending on ``credentials``.
        token
            Optional bearer token used for service authentication.
        env_id
            Chalk environment id to send with requests.
        commit_author
            Optional commit author URN. If omitted, the native client derives
            one from the request credentials.
        extra_headers
            Additional request headers to send with every call.
        chalk_server
            Target Chalk server name.
        credentials
            Optional TLS/SSL credentials marker. If set, schemeless endpoints
            use ``https://``.
        metadata
            Optional default metadata. ``authorization``, ``x-chalk-env-id``,
            and ``x-chalk-server`` are translated into native client settings.
        options
            Accepted for compatibility with the gRPC-backed constructor and
            ignored by the native client.
        """
        if _NativeVolumeClient is None:
            raise VolumeError(
                "volumes require the native chalkcompute._native extension"
            ) from _NATIVE_IMPORT_ERROR
        del options
        if metadata is not None:
            token = token or _metadata_value(metadata, "authorization").removeprefix(
                "Bearer "
            )
            env_id = env_id or _metadata_value(metadata, "x-chalk-env-id") or None
            chalk_server = _metadata_value(metadata, "x-chalk-server") or chalk_server
        endpoint = _native_endpoint(endpoint, use_tls=credentials is not None)
        self._endpoint = endpoint
        self._token = token
        self._env_id = env_id
        self._commit_author = commit_author
        self._extra_headers = builtins.list(extra_headers or [])
        self._chalk_server = chalk_server
        self._connect: ConnectClient | None = None
        self._closed = False
        self._inner = _NativeVolumeClient(
            endpoint,
            token,
            env_id,
            commit_author,
            self._extra_headers,
            chalk_server,
        )

    @classmethod
    def from_connect(cls, connect: ConnectClient) -> VolumeClient:
        """Create a client from an authenticated :class:`ConnectClient`.

        Parameters
        ----------
        connect
            An authenticated ``ConnectClient``. Auth is ensured
            automatically if not already done.

        Returns
        -------
        VolumeClient
            A new client with endpoint, token, and environment settings
            derived from the ``ConnectClient``.
        """
        connect._ensure_auth()
        # No commit_author here on purpose. When the author is unset, the
        # native client derives a chalk:{environment}:agent:{agent_id} URN
        # from the credentials so dashboard commit history links back to
        # the authoring agent.
        client = cls(
            connect._api_server or "",
            token=connect._access_token or "",
            env_id=connect._env_id or None,
            chalk_server="go-api",
        )
        if getattr(connect, "_uses_web_identity_token", lambda: False)() is True:
            client._connect = connect
        return client

    @property
    def _inner(self) -> Any:
        self._sync_web_identity_auth()
        return self._native_inner

    @_inner.setter
    def _inner(self, value: Any) -> None:
        self._native_inner = value

    def _sync_web_identity_auth(self) -> None:
        """Recreate the native client after an identity-token rotation."""
        connect = getattr(self, "_connect", None)
        if connect is None:
            return
        connect._ensure_auth()
        token = connect._access_token or ""
        env_id = connect._env_id or None
        if token == self._token and env_id == self._env_id:
            return
        if _NativeVolumeClient is None:
            raise VolumeError(
                "volumes require the native chalkcompute._native extension"
            ) from _NATIVE_IMPORT_ERROR
        self._token = token
        self._env_id = env_id
        self._native_inner = _NativeVolumeClient(
            self._endpoint,
            token,
            env_id,
            self._commit_author,
            self._extra_headers,
            self._chalk_server,
        )

    def __enter__(self) -> VolumeClient:
        """Enter a context that closes the client on exit."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Close the client on context exit."""
        self.close()

    def close(self) -> None:
        """Close the underlying native client handle."""
        self._closed = True

    @traced("chalkcompute.volume.create")
    def create(self, name: str, *, volume_type: str | None = None) -> Volume:
        """Create a new named volume.

        Parameters
        ----------
        name
            Name for the new volume.
        volume_type
            Optional volume kind, e.g. ``"source_code"`` for volumes that
            hold deployed function source files.

        Returns
        -------
        Volume
            A handle to the newly created volume.
        """
        raw = self._create_volume(name, volume_type=volume_type)
        vol = Volume(name, client=self, create_if_missing=False)
        vol._info = _dict_to_volume_info(raw["volume"])
        return vol

    def from_name(self, name: str) -> Volume:
        """Return a handle by name without validating that it exists.

        Parameters
        ----------
        name
            Name of the existing volume.

        Returns
        -------
        Volume
            A handle that fetches metadata lazily on first use.
        """
        return Volume(name, client=self, create_if_missing=False)

    def lookup(self, name: str) -> Volume:
        """Look up a volume by name, verifying it exists.

        Parameters
        ----------
        name
            Name of the volume.

        Returns
        -------
        Volume
            A handle with metadata pre-fetched.

        Raises
        ------
        VolumeNotFoundError
            If the volume does not exist.
        """
        vol = Volume(name, client=self, create_if_missing=False)
        vol.reload()
        return vol

    def exists(self, name: str) -> bool:
        """Return whether a volume with ``name`` exists.

        Probes ``GetVolume`` with the default version selector. A not-found
        response maps to ``False``; any other client error propagates so a
        transient failure is never mistaken for "absent".
        """
        try:
            self._get_volume(name)
        except VolumeNotFoundError:
            return False
        return True

    @traced("chalkcompute.volume.upload_lazy_files")
    def upload_lazy_files(
        self, image: Image, *, volume_prefix: str = "files"
    ) -> builtins.list[VolumeMount]:
        """Upload an image's lazy local files into volumes.

        Creates one volume per destination directory and commits
        all files for that directory as a single version.

        Parameters
        ----------
        image
            Image whose lazy local files should be uploaded.
        volume_prefix
            Prefix used when naming the generated volumes.

        Returns
        -------
        list of VolumeMount
            One :class:`VolumeMount` per created volume.
        """
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
            volumes.append(VolumeMount(name=vol.name, mount_path=dest_dir))
        return volumes

    @traced("chalkcompute.volume.stage_lazy_files")
    def stage_lazy_files(
        self, image: Image, *, volume_name: str
    ) -> tuple[builtins.list[VolumeMount], builtins.list[dict[str, Any]]]:
        """Stage an image's lazy local files into source-code volumes.

        Reuses (or creates, ``source_code``-kind) one stable volume
        per destination directory — named from ``volume_name`` — and uploads
        the file bytes, but does **not** commit. Each staged upload is
        returned as a ``chalk.volume.v2.CommitIntent`` JSON dict for
        ``CreateExternalFunctionVersionRequest.volume_commits``: the server
        commits the intent and stamps the resulting version_id onto the spec
        volume with the matching name, so every replica is pinned to that
        immutable snapshot.

        On reused volumes the intent carries a recursive remove of root,
        which the server applies as "replace the base tree": the committed
        version is exactly the staged files, so redeploys never leak stale
        files into new versions.

        Parameters
        ----------
        image
            Image whose lazy local files should be staged.
        volume_name
            Stable name for the volume, typically derived from the function
            or class name so redeploys append versions to the same volume.
            A per-directory suffix is added when the image spans multiple
            destination directories.

        Returns
        -------
        tuple of (list of VolumeMount, list of dict)
            One mount and one commit-intent dict per volume.
        """
        from chalkcompute._naming import sanitize_dns_label

        try:
            dir_to_files = image.lazy_local_files_grouped_for_volume_mounts()
        except ValueError as e:
            raise VolumeError(str(e)) from e

        volumes: builtins.list[VolumeMount] = []
        intents: builtins.list[dict[str, Any]] = []
        for dest_dir, files in dir_to_files.items():
            name = sanitize_dns_label(f"{volume_name}-{dest_dir}")
            existed = self.exists(name)
            vol = (
                self.from_name(name)
                if existed
                else self.create(name, volume_type="source_code")
            )
            source = [
                (os.path.relpath(lf.dest, dest_dir), Path(lf.src)) for lf in files
            ]
            self._raise_if_closed()
            try:
                staged = self._inner.stage(vol.name, source)
                intent_bytes = self._inner.staged_intent(vol.name, staged)
            except _native_error_types() as exc:
                _raise_volume_error(exc, vol.name)
            intent = pb_v2.CommitIntent.FromString(bytes(intent_bytes))
            if existed:
                intent.path_deltas.removes.add(path="/", recursive=True)
            intents.append(MessageToDict(intent))
            volumes.append(VolumeMount(name=vol.name, mount_path=dest_dir))
        return volumes, intents

    def delete_volume(self, name: str) -> None:
        """Delete a volume by name.

        Parameters
        ----------
        name
            Volume to delete.
        """
        self._raise_if_closed()
        try:
            self._inner.delete_volume(name)
        except _native_error_types() as exc:
            _raise_volume_error(exc, name)

    def list(self, *, limit: int = 100) -> builtins.list[VolumeInfo]:
        """List all volumes the caller has access to.

        Parameters
        ----------
        limit
            Page size used when traversing the server-side cursor.

        Returns
        -------
        list of VolumeInfo
            One entry per volume.
        """
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
            raise VolumeClosedError("VolumeClient is closed")

    def _create_volume(
        self, name: str, *, volume_type: str | None = None
    ) -> dict[str, Any]:
        self._raise_if_closed()
        try:
            return dict(self._inner.create_volume(name, volume_type))
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
            "chunk_size": _CHUNK_SIZE_BYTES,
            "inline_threshold": _INLINE_THRESHOLD_BYTES,
            "max_pack_bytes": _MAX_PACK_BYTES,
            "file_concurrency": _FILE_CONCURRENCY,
            "chunk_concurrency": _CHUNK_CONCURRENCY,
            "batch_size": batch_size,
            "max_chunk_retries": _CHUNK_MAX_RETRIES,
            "max_rate_limit_retries": _RATE_LIMIT_MAX_RETRIES,
            "max_commit_retries": _COMMIT_MAX_REBASE_RETRIES,
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
            self._inner.download_to_file(
                volume, remote_path, local_path, version_id, None
            )
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


def _read_file_response(resp: pb_v2.GetFileResponse) -> bytes:
    content_type = resp.WhichOneof("content")
    if content_type == "data":
        return resp.data
    if content_type == "chunked":
        chunks = sorted(resp.chunked.chunks, key=lambda c: c.offset)
        if not chunks:
            return b""
        return b"".join(
            _read_file_from_signed_uri(c.signed_download_uri) for c in chunks
        )
    if content_type == "packed":
        pack = resp.packed.pack
        data = _read_file_from_signed_uri(pack.signed_download_uri)
        return data[pack.offset : pack.offset + pack.size]
    raise VolumeError(f"Unsupported file response type: {content_type!r}")


class _BatchUpload:
    """Accumulates volume writes and commits them as one version."""

    def __init__(
        self,
        volume: Volume,
        *,
        max_files_per_commit: int,
    ) -> None:
        self._volume = volume
        self._max_files_per_commit = max_files_per_commit
        self._pending: list[tuple[str, bytes | Path]] = []

    def put(self, path: str, data: bytes | str) -> None:
        """Stage a file for the next commit.

        Parameters
        ----------
        path
            Destination path within the volume.
        data
            File contents (``bytes`` or ``str``).
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._pending.append((path, data))
        if len(self._pending) >= self._max_files_per_commit:
            self._flush()

    def put_file(self, path: str, local_path: str | Path) -> None:
        """Stage a local file for the next commit.

        Parameters
        ----------
        path
            Destination path within the volume.
        local_path
            Local file to upload.
        """
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


# ---------------------------------------------------------------------------
# Mount helpers
# ---------------------------------------------------------------------------


def normalize_volume_refs(
    volumes: Iterable[tuple[str, str]] | None,
) -> builtins.list[tuple[str, str]]:
    """Validate caller-supplied ``(name, mount_path)`` volume references.

    Shared by every surface that accepts ``volumes=[...]`` so the accepted
    shape can't drift between sandboxes, containers, functions, classes, and
    scaling groups. Existence is checked later, at run/deploy time, by
    :func:`referenced_volume_mount`.
    """
    refs: builtins.list[tuple[str, str]] = []
    for item in volumes or []:
        if not (
            isinstance(item, (tuple, list))
            and len(item) == 2
            and all(isinstance(s, str) for s in item)
        ):
            raise ValueError(
                f"Each volumes entry must be a (name, mount_path) string tuple, "
                f"got {item!r}. Example: volumes=[('my-volume', '/data')]"
            )
        refs.append((item[0], item[1]))
    return refs


def referenced_volume_mount(
    connect: ConnectClient, name: str, mount_path: str
) -> VolumeMount:
    """Build a mount for a caller-referenced volume, failing fast if absent.

    A volume supplied as a ``(name, mount_path)`` tuple is owned by the caller
    and must already exist. Checking here turns a typo'd name into a deploy-time
    error instead of a container that mounts nothing.
    """
    if not VolumeClient.from_connect(connect).exists(name):
        raise VolumeError(
            f"Referenced volume {name!r} was not found. "
            f"Check the name, or create it before referencing it."
        )
    return VolumeMount(name=name, mount_path=mount_path)


def cleanup_owned_volumes(
    client: VolumeClient,
    all_volumes: Iterable[VolumeMount],
    referenced_volumes: Iterable[tuple[str, str]],
) -> None:
    """Delete only volumes the SDK created; never user-referenced ones.

    ``all_volumes`` contains both volumes the SDK uploaded (e.g. for an
    image's lazy local files) and volumes the caller referenced via
    ``volumes=[(name, path)]``. Caller-referenced volumes are owned by
    the caller and must outlive this lifecycle.
    """
    referenced_names = {name for name, _ in referenced_volumes}
    for v in all_volumes:
        if v.name in referenced_names:
            continue
        client.delete_volume(v.name)
