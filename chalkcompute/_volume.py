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

import base64
import builtins
import datetime
import importlib
import logging
import math
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf import Oneof

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.manager.v1 import dataset_download_connect
from chalkcompute._gen.chalk.manager.v1 import dataset_download_pb
from chalkcompute._gen.chalk.volume.v2 import volume_connect
from chalkcompute._gen.chalk.volume.v2 import volume_pb as pb_v2
from chalkcompute._http import HTTP_TRANSPORT_ERRORS, SYNC_HTTP_CLIENT
from chalkcompute._image import Image
from chalkcompute._tracing import traced

logger = logging.getLogger(__name__)

#: Mount type stamped into container/sandbox specs for every Chalk volume.
MOUNT_TYPE = "versioned_chalkfs"

_SHARED_SOURCE_VOLUME_PREFIX = "cc-image-"
_WORKLOAD_FORK_PREFIX = "cc-w-"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VolumeError(Exception):
    """Base exception for volume errors."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


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


@dataclass
class ForkInfo:
    """Metadata for a volume fork.

    A fork is an independent version lineage created by :meth:`Volume.fork`.
    """

    name: str
    version_id: int
    commit_id: str | None = None
    updated_at: datetime.datetime | None = None
    parent_version_id: int | None = None


@dataclass(frozen=True)
class VolumeMount:
    """A volume to mount into a container, sandbox, or scaling group.

    Attributes
    ----------
    name
        Volume name.
    mount_path
        Absolute path inside the container where the volume is mounted.
    version_id
        Optional immutable version to mount.
    ref_name
        Optional mutable fork to mount.
    """

    name: str
    mount_path: str
    version_id: int | None = None
    ref_name: str | None = None

    def __post_init__(self) -> None:
        if self.ref_name == "":
            raise ValueError("ref_name must not be empty")
        if self.version_id is not None and self.ref_name is not None:
            raise ValueError("version_id and ref_name cannot both be set")

    def to_spec_dict(self) -> dict[str, str | int]:
        """Render this mount as a container-spec ``volumes`` entry."""
        spec: dict[str, str | int] = {
            "name": self.name,
            "mountPath": self.mount_path,
            "type": MOUNT_TYPE,
        }
        if self.version_id is not None:
            spec["versionId"] = self.version_id
        if self.ref_name is not None:
            spec["refName"] = self.ref_name
        return spec


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
        response = SYNC_HTTP_CLIENT.get(signed_download_uri, timeout=300)
    except TimeoutError as e:
        raise VolumeError(f"Timed out downloading signed file URL: {e}") from e
    except HTTP_TRANSPORT_ERRORS as e:
        raise VolumeError(f"Signed file download failed: {e}") from e
    if not 200 <= response.status < 300:
        raise VolumeError(f"Signed file download failed with status {response.status}")
    return response.content


_CHUNK_SIZE_BYTES: int = 8 * 1024 * 1024
_MAX_PACK_BYTES: int = 64 * 1024 * 1024
_COMMIT_MAX_REBASE_RETRIES: int = 3
_TRANSFER_MAX_WORKERS: int = 32
_FILE_CONCURRENCY: int = _TRANSFER_MAX_WORKERS
_CHUNK_CONCURRENCY: int = _TRANSFER_MAX_WORKERS
_CHUNK_MAX_RETRIES: int = 3
_RATE_LIMIT_MAX_RETRIES: int = 8
_BATCH_MAX_FILES: int = 200
_DATASET_INGEST_MAX_FILES: int = 500
_DATASET_INGEST_MAX_BYTES: int = 4 * 1024 * 1024 * 1024


def _dataset_output_path(object_path: str, dest_prefix: str = "") -> str:
    """Map a dataset output object path to a volume path."""
    _, separator, relative = object_path.partition("/outputs/")
    if not separator or not relative:
        raise ValueError(f"invalid dataset output path {object_path!r}")
    relative_parts = relative.split("/")
    prefix = dest_prefix.strip("/")
    prefix_parts = prefix.split("/") if prefix else []
    if any(part in ("", ".", "..") for part in [*prefix_parts, *relative_parts]):
        raise ValueError(f"invalid dataset output path {object_path!r}")
    return "/".join([*prefix_parts, *relative_parts])


def _default_upload_config() -> dict[str, int]:
    return {
        "chunk_size": _CHUNK_SIZE_BYTES,
        "max_pack_bytes": _MAX_PACK_BYTES,
        "file_concurrency": _FILE_CONCURRENCY,
        "chunk_concurrency": _CHUNK_CONCURRENCY,
        "batch_size": _BATCH_MAX_FILES,
        "max_chunk_retries": _CHUNK_MAX_RETRIES,
        "max_rate_limit_retries": _RATE_LIMIT_MAX_RETRIES,
        "max_commit_retries": _COMMIT_MAX_REBASE_RETRIES,
    }


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
    _NativeVolumeClient = getattr(_native, "VolumeNativeClient", None)
    _NativeVolumeRpcError = getattr(_native, "VolumeRpcError", None)
    _NATIVE_IMPORT_ERROR = None


@dataclass
class VersionInfo:
    """Metadata for an immutable volume snapshot.

    Attributes
    ----------
    id
        Stable identifier of this version.
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
    def ref(self) -> str:
        """Mutable ref tracked by this handle."""
        return self._ref

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
            response = self._client._get_volume(
                self._name,
                version_id=self._version_id,
                ref_name=self._selector_ref(),
            )
        except VolumeNotFoundError:
            if not self._create_if_missing:
                raise
            try:
                created = self._client._create_volume(self._name)
            except VolumeError as exc:
                if exc.code != "already_exists":
                    raise
                # Another caller can create the volume after our lookup. Read
                # its metadata with the original selector; do not retry creation.
                response = self._client._get_volume(
                    self._name,
                    version_id=self._version_id,
                    ref_name=self._selector_ref(),
                )
            else:
                if created.volume is None:
                    raise VolumeError("CreateVolume returned no volume metadata")
                return _proto_to_volume_info(created.volume), None

        if response.volume is None:
            raise VolumeError("GetVolume returned no volume metadata")
        return (
            _proto_to_volume_info(response.volume),
            (
                _proto_to_version_info(response.version)
                if response.version is not None
                else None
            ),
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
            ref_name=self._selector_ref(),
        )
        self._update_version_from_statuses(statuses)
        return self

    @traced("chalkcompute.volume.put_dir")
    def put_dir(self, local_path: str | Path, remote_path: str = "") -> Volume:
        """Upload a directory, overwriting files without deleting other content.

        Parameters
        ----------
        local_path : str or pathlib.Path
            Local directory to upload recursively.
        remote_path : str, optional
            Relative destination within the volume. Defaults to its root.

        Returns
        -------
        Volume
            ``self``, with its cached version updated after commits complete.

        Raises
        ------
        ValueError
            If paths are invalid or the directory contains symlinks or special
            files.
        VolumeError
            If the volume is closed, pinned, or an upload fails.

        Notes
        -----
        Empty directories are omitted. Uploads may make multiple commits.
        A failed upload can leave earlier commits visible.
        """
        self._raise_if_closed()
        self._ensure_mutable()
        root = Path(local_path)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("local_path must be a directory, not a symlink")
        destination = PurePosixPath(remote_path)
        if (
            destination.is_absolute()
            or ".." in destination.parts
            or "\\" in remote_path
        ):
            raise ValueError(
                "remote_path must be relative and cannot contain traversal"
            )
        files: list[tuple[str, bytes | Path]] = []

        def scan(directory: Path) -> None:
            for path in sorted(directory.iterdir()):
                if path.is_symlink():
                    raise ValueError(f"Symlinks are not supported: {path}")
                if path.is_dir():
                    scan(path)
                elif path.is_file():
                    files.append(
                        (str(destination / path.relative_to(root).as_posix()), path)
                    )
                else:
                    raise ValueError(f"Special files are not supported: {path}")

        scan(root)
        if files:
            self._ensure_ready()
            statuses = self._client._upload(
                self.name, files, ref_name=self._selector_ref()
            )
            self._update_version_from_statuses(statuses)
        return self

    @traced("chalkcompute.volume.load_git_repo")
    def load_git_repo(
        self,
        url: str,
        *,
        dest_prefix: str = "",
        revision: str = "main",
    ) -> Volume:
        """Clone a Git repository and upload its checkout to this volume.

        Requires the ``git`` executable on the local machine. The checkout
        includes Git metadata so it can be used as a working repository after
        mounting the volume.

        Parameters
        ----------
        url
            Repository URL accepted by ``git clone``.
        dest_prefix
            Destination directory within the volume.
        revision
            Branch, tag, or commit SHA to check out.
        """
        self._ensure_mutable()
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        if not isinstance(dest_prefix, str):
            raise TypeError("dest_prefix must be a string")
        prefix_parts = [part for part in dest_prefix.strip("/").split("/") if part]
        if any(part in (".", "..") for part in prefix_parts):
            raise ValueError("dest_prefix must not contain '.' or '..'")
        if not isinstance(revision, str) or not revision.strip():
            raise ValueError("revision must be a non-empty string")

        with tempfile.TemporaryDirectory(prefix="chalkcompute-git-") as temp_dir:
            checkout = Path(temp_dir) / "checkout"
            from chalkcompute._git import GitError, unpack_remote_to_mount

            try:
                unpack_remote_to_mount(url, checkout, revision)
            except GitError as exc:
                raise VolumeError(str(exc)) from exc

            files: list[tuple[str, Path]] = []
            for local_path in checkout.rglob("*"):
                relative_path = local_path.relative_to(checkout)
                if local_path.is_symlink():
                    raise VolumeError(
                        f"git clone contains unsupported symbolic link {relative_path!s}"
                    )
                if local_path.is_file():
                    files.append(
                        ("/".join([*prefix_parts, *relative_path.parts]), local_path)
                    )

            if not files:
                raise VolumeError("git clone produced no files to upload")
            self._ensure_ready()
            statuses = self._client._upload(
                self._name,
                files,
                ref_name=self._selector_ref(),
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
            ref_name=self._selector_ref(),
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

    @traced("chalkcompute.volume.load_dataset")
    def load_dataset(
        self,
        source: str | object,
        *,
        dest_prefix: str = "",
        timeout: float = 600.0,
        max_attempts: int = 3,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> VersionInfo:
        """Copy a Chalk Dataset revision into a volume.

        Only the revision's output parquet files are ingested with
        matching partitioning.

        Parameters
        ----------
        source
            Revision id or an object with a ``revision_id`` attribute.
        dest_prefix
            Optional destination directory within the volume.
        timeout
            Deadline in seconds for each dataset download-URI stream.
        max_attempts
            Number of full stream passes after staging failures. Each new pass
            obtains fresh signed URLs and skips files already staged.
        on_progress
            Optional callback receiving cumulative staged file and byte counts.

        Returns
        -------
        VersionInfo
            The newly committed volume version.
        """
        self._ensure_mutable()
        revision_id = (
            source if isinstance(source, str) else getattr(source, "revision_id", None)
        )
        if not isinstance(revision_id, str) or not revision_id.strip():
            raise ValueError("source must be a revision id or have a revision_id")
        if not isinstance(dest_prefix, str):
            raise TypeError("dest_prefix must be a string")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number")
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts <= 0
        ):
            raise ValueError("max_attempts must be a positive integer")
        if on_progress is not None and not callable(on_progress):
            raise TypeError("on_progress must be callable")

        self._ensure_ready()
        revision_id = revision_id.strip()
        staged: Any | None = None
        staged_paths: set[str] = set()
        staged_files = 0
        staged_bytes = 0
        stub = self._client._connect.stub(
            dataset_download_connect.DatasetDownloadServiceClientSync
        )

        for attempt in range(1, max_attempts + 1):
            batch: list[tuple[str, str, int]] = []
            batch_bytes = 0

            def stage_batch() -> None:
                nonlocal batch, batch_bytes, staged, staged_files, staged_bytes
                if not batch:
                    return
                next_staged = self._client._inner.stage_remote(
                    self._name,
                    batch,
                    None,
                )
                if staged is None:
                    staged = next_staged
                else:
                    staged.extend(next_staged)
                staged_paths.update(path for path, _, _ in batch)
                staged_files += len(batch)
                staged_bytes += batch_bytes
                batch = []
                batch_bytes = 0
                if on_progress is not None:
                    on_progress(staged_files, staged_bytes)

            try:
                stream = self._client._connect.call(
                    stub.stream_dataset_download_ur_is,
                    dataset_download_pb.StreamDatasetDownloadURIsRequest(
                        revision_id=revision_id
                    ),
                    timeout=float(timeout),
                )
                for entry in stream:
                    path = _dataset_output_path(entry.object_path, dest_prefix)
                    if path in staged_paths:
                        continue
                    if entry.size_bytes < 0:
                        raise VolumeError(
                            f"Dataset download returned a negative size for {path!r}"
                        )
                    batch.append((path, entry.signed_url, entry.size_bytes))
                    batch_bytes += entry.size_bytes
                    if (
                        len(batch) >= _DATASET_INGEST_MAX_FILES
                        or batch_bytes >= _DATASET_INGEST_MAX_BYTES
                    ):
                        stage_batch()
                stage_batch()
                break
            except _native_error_types() as exc:
                if attempt == max_attempts:
                    _raise_volume_error(exc, self._name)
                logger.warning(
                    "Dataset load staging failed on attempt %d/%d: %s",
                    attempt,
                    max_attempts,
                    _native_error_message(exc),
                )
            except ConnectRpcError as exc:
                if attempt == max_attempts:
                    _raise_connect_volume_error(exc, self._name)
                logger.warning(
                    "Dataset load stream failed on attempt %d/%d: %s",
                    attempt,
                    max_attempts,
                    exc,
                )

        if staged is None:
            raise VolumeError(
                f"Dataset revision {revision_id!r} yielded no output files"
            )
        try:
            status = self._client._inner.commit_staged(
                self._name,
                staged,
                None,
                _COMMIT_MAX_REBASE_RETRIES,
                self._selector_ref(),
            )
        except _native_error_types() as exc:
            _raise_volume_error(exc, self._name)
        return self._update_version_from_statuses([_dict_to_commit_status(status)])

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
            response = self._client._list_volume_versions(
                self._name,
                limit=limit,
                cursor=cursor,
                ref_name=self._selector_ref(),
            )
            versions.extend(_proto_to_version_info(v) for v in response.versions)
            cursor = response.next_cursor
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
        response = self._client._get_volume(self._name, ref_name=self._selector_ref())
        if response.volume is None:
            raise VolumeError("GetVolume returned no volume metadata")
        self._info = _proto_to_volume_info(response.volume)
        if response.version is None:
            raise VolumeError(f"Volume {self._name!r} has no latest version")
        self._version = _proto_to_version_info(response.version)
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

    def at_ref(self, name: str) -> Volume:
        """Return a mutable handle tracking an existing ref.

        Parameters
        ----------
        name
            Name of the ref to open.

        Returns
        -------
        Volume
            A mutable handle tracking ``name``.
        """
        self._raise_if_closed()
        if not name:
            raise ValueError("ref name must not be empty")
        volume = Volume(
            self._name,
            client=self._client,
            ref=name,
            create_if_missing=False,
        )
        volume._info = self._info
        return volume

    @traced("chalkcompute.volume.fork")
    def fork(self, name: str) -> Volume:
        """Create an independent ref at this handle's current version.

        The returned handle reads and writes through the new ref. Forking a
        version-pinned handle starts exactly at that immutable version.

        Parameters
        ----------
        name
            Name for the new ref.

        Returns
        -------
        Volume
            A mutable handle tracking the new ref.
        """
        self._raise_if_closed()
        from_version_id = self._version_id
        if from_version_id is None:
            from_version_id = self.version.id
        ref = self._client._create_ref(self._name, name, from_version_id)
        forked = Volume(
            self._name,
            client=self._client,
            ref=ref.name,
            create_if_missing=False,
        )
        forked._info = self._info
        forked._version = _ref_to_version_info(ref)
        return forked

    def forks(self) -> list[ForkInfo]:
        """List the forks for this volume."""
        self._ensure_ready()
        return [
            fork for fork in self._client._list_refs(self._name) if fork.name != "main"
        ]

    @traced("chalkcompute.volume.delete_fork")
    def delete_fork(self) -> None:
        """Delete this handle's fork without deleting its versions.

        Raises
        ------
        VolumeError
            If this handle tracks the main ref or is pinned to a version.
        """
        self._raise_if_closed()
        if self._version_id is not None:
            raise VolumeError("A version-pinned handle does not identify a ref")
        if not self._ref or self._ref == "main":
            raise VolumeError("The main ref cannot be deleted")
        self._client._delete_ref(self._name, self._ref)

    def mount(self, mount_path: str | None = None) -> VolumeMount:
        """Create a mount specification for this handle.

        Fork handles mount their mutable ref. Version-pinned handles mount an
        immutable snapshot.
        The default path is ``/volumes/<volume-name>``.
        """
        self._raise_if_closed()
        path = mount_path or f"/volumes/{self._name}"
        return VolumeMount(
            name=self._name,
            mount_path=path,
            version_id=self._version_id,
            ref_name=self._selector_ref(),
        )

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
            ref_name=self._selector_ref(),
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
            response = self._client._list_files(
                self._name,
                path=path,
                version_id=self._version_id,
                ref_name=self._selector_ref(),
                limit=1000,
                cursor=cursor,
            )
            files.extend(_proto_to_file_info(f) for f in response.files)
            if response.version is not None:
                self._version = _proto_to_version_info(response.version)
            cursor = response.next_cursor
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
        """Initialize the typed volume control plane and native data plane.

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
        self._extra_headers = builtins.list(extra_headers or [])
        self._chalk_server = chalk_server
        self._connect = _connect_client_for_endpoint(
            endpoint,
            token=token,
            env_id=env_id,
        )
        self._owns_connect = True
        self._closed = False
        self._inner = _NativeVolumeClient(
            endpoint,
            token,
            env_id,
            commit_author,
            self._extra_headers,
            chalk_server,
            self._refresh_token,
        )

    @classmethod
    def from_env(cls, *, chalk_client: Any | None = None) -> VolumeClient:
        """Create a volume client using environment authentication.

        Parameters
        ----------
        chalk_client : Any, optional
            Chalk client supplying authentication. Defaults to environment auth.

        Returns
        -------
        VolumeClient
            Client that owns its connection and closes it on context exit or
            an explicit :meth:`close` call.
        """
        client = cls.from_connect(ConnectClient(chalk_client=chalk_client))
        client._owns_connect = True
        return client

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
        client._connect = connect
        client._owns_connect = False
        return client

    def _refresh_token(self) -> str:
        """Refresh the bearer token used by the native client."""
        self._connect._ensure_auth()
        return self._connect._access_token or ""

    def __enter__(self) -> VolumeClient:
        """Enter a context that closes the client on exit."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Close the client on context exit."""
        self.close()

    def close(self) -> None:
        """Close owned RPC resources and reject subsequent operations."""
        if not self._closed and self._owns_connect:
            self._connect.close()
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
        response = self._create_volume(name, volume_type=volume_type)
        if response.volume is None:
            raise VolumeError("CreateVolume returned no volume metadata")
        vol = Volume(name, client=self, create_if_missing=False)
        vol._info = _proto_to_volume_info(response.volume)
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
        self, image: Image, *, volume_prefix: str = "files", fork: bool = False
    ) -> builtins.list[VolumeMount]:
        """Resolve local files to source snapshots or owned workload forks.

        Uses one stable volume per image recipe, also used by Image.build().
        Each destination directory mounts its own resolved version of that volume.

        Existing content is reused without re-uploading. New versions replace
        the entire tree. Shared source volumes survive workload cleanup.

        Parameters
        ----------
        image
            Image whose lazy local files should be resolved.
        volume_prefix
            Retained for compatibility. Volume names depend only on the image spec.
        fork
            Create an owned mutable fork per mount for workload cleanup.

        Returns
        -------
        list of VolumeMount
            One source mount per destination directory, pinned unless fork is set.
        """
        from chalkcompute._image_build import default_source_volume_name

        mounts = self._resolve_lazy_files(
            image, volume_name=default_source_volume_name(image), shared=True
        )
        if not fork:
            return mounts
        forked: builtins.list[VolumeMount] = []
        try:
            for mount in mounts:
                assert mount.version_id is not None
                token = base64.b32encode(uuid.uuid4().bytes).decode().rstrip("=").lower()
                ref_name = _WORKLOAD_FORK_PREFIX + token
                self._create_ref(mount.name, ref_name, mount.version_id)
                forked.append(
                    VolumeMount(mount.name, mount.mount_path, ref_name=ref_name)
                )
        except BaseException:
            for mount in reversed(forked):
                try:
                    assert mount.ref_name is not None
                    self._delete_ref(mount.name, mount.ref_name)
                except Exception:
                    logger.warning(
                        "Could not delete failed upload fork %s",
                        mount.ref_name,
                        exc_info=True,
                    )
            raise
        return forked

    @traced("chalkcompute.volume.stage_lazy_files")
    def stage_lazy_files(
        self, image: Image, *, volume_name: str
    ) -> tuple[builtins.list[VolumeMount], builtins.list[pb_v2.CommitIntent]]:
        """Stage an image's lazy local files into source-code volumes.

        Reuses (or creates, ``source_code``-kind) one stable volume
        per destination directory — named from ``volume_name`` — and uploads
        the file bytes, but does **not** commit. Each staged upload is
        returned as a typed ``chalk.volume.v2.CommitIntent`` for
        ``ExternalFunctionVersionSpec.volume_commits``: the server
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
        tuple of (list of VolumeMount, list of CommitIntent)
            One mount and one commit-intent dict per volume.
        """
        from chalkcompute._naming import sanitize_dns_label

        try:
            dir_to_files = image.lazy_local_files_grouped_for_volume_mounts()
        except ValueError as e:
            raise VolumeError(str(e)) from e

        volumes: builtins.list[VolumeMount] = []
        intents: builtins.list[pb_v2.CommitIntent] = []
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
            removes = [("/", True)] if existed else []
            try:
                staged = self._inner.stage(
                    vol.name,
                    source,
                    _default_upload_config(),
                )
                intent_bytes = self._inner.staged_intent(
                    vol.name,
                    staged,
                    None,
                    removes,
                )
            except _native_error_types() as exc:
                _raise_volume_error(exc, vol.name)
            intent = pb_v2.CommitIntent.from_binary(bytes(intent_bytes))
            intents.append(intent)
            volumes.append(VolumeMount(name=vol.name, mount_path=dest_dir))
        return volumes, intents

    @traced("chalkcompute.volume.resolve_lazy_files")
    def _resolve_lazy_files(
        self,
        image: Image,
        *,
        volume_name: str,
        shared: bool = False,
        timeout: float = 600.0,
        poll_interval: float = 1.0,
    ) -> builtins.list[VolumeMount]:
        """Resolve lazy local files to immutable source volume mounts."""
        from chalkcompute._naming import sanitize_dns_label

        try:
            groups = image.lazy_local_files_grouped_for_volume_mounts()
        except ValueError as exc:
            raise VolumeError(str(exc)) from exc

        deadline = time.monotonic() + timeout
        mounts: builtins.list[VolumeMount] = []
        for mount_path, files in groups.items():
            if shared:
                name = volume_name
            else:
                name = (
                    volume_name
                    if mount_path == "/app"
                    else sanitize_dns_label(f"{volume_name}-{mount_path}")
                )
            source: builtins.list[tuple[Any, ...]] = []
            for file in files:
                path = os.path.relpath(file.dest, mount_path).replace(os.sep, "/")
                entry: tuple[Any, ...] = (path, Path(file.src))
                if file.mode is not None:
                    entry = (*entry, {"mode": file.mode})
                source.append(entry)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VolumeError("Source file resolution timed out")
            self._raise_if_closed()
            try:
                prepared = self._inner.prepare_files(source, "main")
                version = self._inner.resolve_prepared_files(
                    name,
                    "source_code",
                    prepared,
                    {
                        "upload": _default_upload_config(),
                        "timeout_ms": max(1, int(remaining * 1_000)),
                        "poll_interval_ms": max(1, int(poll_interval * 1_000)),
                    },
                )
            except _native_error_types() as exc:
                _raise_volume_error(exc, name)
            version_id = version.get("version_id")
            if not isinstance(version_id, int):
                raise VolumeError("Source file resolution returned no version")
            mounts.append(
                VolumeMount(
                    name=name,
                    mount_path=mount_path,
                    version_id=version_id,
                )
            )
        return mounts

    def delete_volume(self, name: str) -> None:
        """Delete a volume by name.

        Parameters
        ----------
        name
            Volume to delete.
        """
        self._call(
            self._stub().delete_volume,
            pb_v2.DeleteVolumeRequest(volume=pb_v2.VolumeRef(name=name)),
            volume_name=name,
        )

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
            response = self._list_volumes(limit=limit, cursor=cursor)
            volumes.extend(_proto_to_volume_info(v) for v in response.volumes)
            cursor = response.next_cursor
            if not cursor:
                return volumes

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise VolumeClosedError("VolumeClient is closed")

    def _call(
        self,
        method: Any,
        request: Any,
        *,
        volume_name: str,
    ) -> Any:
        self._raise_if_closed()
        headers = dict(self._extra_headers)
        headers["x-chalk-server"] = self._chalk_server
        try:
            return self._connect.call(
                method,
                request,
                extra_headers=headers,
            )
        except ConnectRpcError as exc:
            _raise_connect_volume_error(exc, volume_name)

    def _stub(self) -> volume_connect.VolumeServiceClientSync:
        self._raise_if_closed()
        return self._connect.stub(volume_connect.VolumeServiceClientSync)

    def _create_volume(
        self, name: str, *, volume_type: str | None = None
    ) -> pb_v2.CreateVolumeResponse:
        return self._call(
            self._stub().create_volume,
            pb_v2.CreateVolumeRequest(
                name=name,
                volume_type=_volume_kind(volume_type),
            ),
            volume_name=name,
        )

    def _get_volume(
        self,
        name: str,
        *,
        version_id: int | None = None,
        ref_name: str | None = None,
    ) -> pb_v2.GetVolumeResponse:
        return self._call(
            self._stub().get_volume,
            pb_v2.GetVolumeRequest(
                volume=pb_v2.VolumeRef(name=name),
                selector=_version_selector(version_id, ref_name),
            ),
            volume_name=name,
        )

    def _list_volumes(self, *, limit: int, cursor: str) -> pb_v2.ListVolumesResponse:
        return self._call(
            self._stub().list_volumes,
            pb_v2.ListVolumesRequest(limit=limit, cursor=cursor),
            volume_name="",
        )

    def _list_volume_versions(
        self,
        name: str,
        *,
        limit: int,
        cursor: str,
        ref_name: str | None,
    ) -> pb_v2.ListVolumeVersionsResponse:
        return self._call(
            self._stub().list_volume_versions,
            pb_v2.ListVolumeVersionsRequest(
                volume=pb_v2.VolumeRef(name=name),
                limit=limit,
                cursor=cursor,
                ref=ref_name,
            ),
            volume_name=name,
        )

    def _create_ref(self, volume: str, name: str, from_version_id: int) -> ForkInfo:
        response = self._call(
            self._stub().create_ref,
            pb_v2.CreateRefRequest(
                volume=pb_v2.VolumeRef(name=volume),
                name=name,
                from_version_id=from_version_id,
            ),
            volume_name=volume,
        )
        if response.ref is None:
            raise VolumeError("CreateRef returned no ref metadata")
        return _proto_to_ref_info(response.ref)

    def _list_refs(self, volume: str) -> builtins.list[ForkInfo]:
        response = self._call(
            self._stub().list_refs,
            pb_v2.ListRefsRequest(volume=pb_v2.VolumeRef(name=volume)),
            volume_name=volume,
        )
        return [_proto_to_ref_info(ref) for ref in response.refs]

    def _delete_ref(self, volume: str, name: str) -> None:
        self._call(
            self._stub().delete_ref,
            pb_v2.DeleteRefRequest(
                volume=pb_v2.VolumeRef(name=volume),
                name=name,
            ),
            volume_name=volume,
        )

    def _list_files(
        self,
        volume: str,
        *,
        path: str,
        version_id: int | None,
        ref_name: str | None,
        limit: int,
        cursor: str,
    ) -> pb_v2.ListFilesResponse:
        return self._call(
            self._stub().list_files,
            pb_v2.ListFilesRequest(
                volume=pb_v2.VolumeRef(name=volume),
                path=path,
                recursive=False,
                limit=limit,
                cursor=cursor,
                selector=_version_selector(version_id, ref_name),
            ),
            volume_name=volume,
        )

    def _upload(
        self,
        volume: str,
        files: Sequence[tuple[str, bytes | Path]],
        *,
        ref_name: str | None = None,
    ) -> builtins.list[CommitStatus]:
        self._raise_if_closed()
        if not files:
            return []
        try:
            statuses = self._inner.upload(
                volume,
                builtins.list(files),
                _default_upload_config(),
                ref_name,
            )
        except _native_error_types() as exc:
            _raise_volume_error(exc, volume)
        return [_dict_to_commit_status(status) for status in statuses]

    def _remove_files(
        self,
        volume: str,
        paths: builtins.list[tuple[str, bool]],
        *,
        max_commit_retries: int,
        ref_name: str | None = None,
    ) -> CommitStatus:
        self._raise_if_closed()
        try:
            status = self._inner.remove_files(
                volume,
                paths,
                max_commit_retries,
                ref_name,
            )
        except _native_error_types() as exc:
            _raise_volume_error(exc, volume)
        return _dict_to_commit_status(status)

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
            _raise_volume_error(exc, volume, file_not_found=True)

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
            _raise_volume_error(exc, volume, file_not_found=True)

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


def _proto_to_volume_info(
    info: pb_v2.VolumeInfo | pb_v2.ListedVolume,
) -> VolumeInfo:
    return VolumeInfo(
        name=info.name,
        created_at=_timestamp_to_datetime(info.created_at),
        volume_id=(
            _optional_str(info.volume_id)
            if isinstance(info, pb_v2.VolumeInfo)
            else None
        ),
        access_mode=(
            _volume_access_mode_name(info.access_mode)
            if isinstance(info, pb_v2.VolumeInfo)
            else None
        ),
        ref=(_optional_str(info.ref) if isinstance(info, pb_v2.VolumeInfo) else None),
    )


def _proto_to_version_info(info: pb_v2.VersionInfo) -> VersionInfo:
    return VersionInfo(
        id=info.version_id,
        parent_id=info.parent_id if info.has_field("parent_id") else None,
        created_at=_timestamp_to_datetime(info.created_at),
        ref=_optional_str(info.ref),
        commit_id=_optional_str(info.commit_id),
    )


def _proto_to_ref_info(info: pb_v2.RefInfo) -> ForkInfo:
    return ForkInfo(
        name=info.name,
        version_id=info.version_id,
        commit_id=_optional_str(info.commit_id),
        updated_at=_timestamp_to_datetime(info.updated_at),
        parent_version_id=(
            info.parent_version_id if info.has_field("parent_version_id") else None
        ),
    )


def _ref_to_version_info(ref: ForkInfo) -> VersionInfo:
    return VersionInfo(
        id=ref.version_id,
        created_at=ref.updated_at,
        ref=ref.name,
        commit_id=ref.commit_id,
    )


def _proto_to_file_info(info: pb_v2.FileInfo) -> FileInfo:
    return FileInfo(
        path=info.path,
        size=info.size,
        updated_at=_timestamp_to_datetime(info.updated_at),
        kind=_file_kind_name(info.kind),
        hash=_optional_str(info.hash),
        mode=info.mode if info.has_field("mode") else None,
        uid=info.uid,
        gid=info.gid,
        nlink=info.nlink,
    )


def _ms_to_datetime(value: int | None) -> datetime.datetime | None:
    if value is None:
        return None
    return datetime.datetime.fromtimestamp(value / 1000, tz=datetime.timezone.utc)


def _dict_to_version_info(info: dict[str, Any]) -> VersionInfo:
    return VersionInfo(
        id=info["version_id"],
        parent_id=info.get("parent_id"),
        created_at=_ms_to_datetime(info.get("created_at_ms")),
        ref=_optional_str(info.get("ref")),
        commit_id=_optional_str(info.get("commit_id")),
    )


def _dict_to_commit_status(status: dict[str, Any]) -> CommitStatus:
    return CommitStatus(
        commit_id=status["commit_id"],
        result=status["result"],
        version=(
            _dict_to_version_info(status["version"])
            if status.get("version") is not None
            else None
        ),
        latest_version=(
            _dict_to_version_info(status["latest_version"])
            if status.get("latest_version") is not None
            else None
        ),
        created_at=_ms_to_datetime(status.get("created_at_ms")),
    )


def _timestamp_to_datetime(value: Any) -> datetime.datetime | None:
    return None if value is None else value.to_datetime()


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


def _connect_client_for_endpoint(
    endpoint: str,
    *,
    token: str,
    env_id: str | None,
) -> ConnectClient:
    connect = ConnectClient()
    connect._api_server = endpoint
    connect._access_token = token
    connect._env_id = env_id
    return connect


def _version_selector(
    version_id: int | None,
    ref_name: str | None,
) -> pb_v2.VersionSelector | None:
    if version_id is not None and ref_name is not None:
        raise ValueError("version_id and ref_name are mutually exclusive")
    if version_id is not None:
        return pb_v2.VersionSelector(selector=Oneof("version_id", version_id))
    if ref_name is not None:
        return pb_v2.VersionSelector(selector=Oneof("ref", ref_name))
    return None


def _volume_kind(value: str | None) -> pb_v2.VolumeKind:
    if value is None:
        return pb_v2.VolumeKind.UNSPECIFIED
    if value == "source_code":
        return pb_v2.VolumeKind.SOURCE_CODE
    raise ValueError(f"unknown volume_type {value!r}")


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


def _raise_volume_error(
    exc: BaseException,
    volume_name: str,
    *,
    file_not_found: bool = False,
) -> NoReturn:
    code = _native_error_code(exc)
    message = _native_error_message(exc)
    if code == "not_found":
        if file_not_found:
            raise VolumeFileNotFoundError(message, code=code) from exc
        raise VolumeNotFoundError(message, code=code) from exc
    raise VolumeError(message, code=code) from exc


def _raise_connect_volume_error(
    exc: ConnectRpcError,
    volume_name: str,
) -> NoReturn:
    del volume_name
    code = exc.code.name.lower()
    if exc.code == Code.NOT_FOUND:
        raise VolumeNotFoundError(str(exc), code=code) from exc
    raise VolumeError(str(exc), code=code) from exc


def _volume_access_mode_name(value: pb_v2.VolumeAccessMode) -> str | None:
    return {
        pb_v2.VolumeAccessMode.READ_ONLY: "read_only",
        pb_v2.VolumeAccessMode.READ_WRITE: "read_write",
    }.get(value)


def _file_kind_name(value: pb_v2.FileKind) -> str:
    return {
        pb_v2.FileKind.FILE: "file",
        pb_v2.FileKind.DIRECTORY: "directory",
        pb_v2.FileKind.SYMLINK: "symlink",
    }.get(value, "unspecified")


def _read_file_response(resp: pb_v2.GetFileResponse) -> bytes:
    match resp.content:
        case Oneof(field="data", value=data):
            return data
        case Oneof(field="chunked", value=chunked):
            chunks = sorted(chunked.chunks, key=lambda c: c.offset)
            return b"".join(
                _read_file_from_signed_uri(c.signed_download_uri) for c in chunks
            )
        case Oneof(field="packed", value=packed) if packed.pack is not None:
            pack = packed.pack
            data = _read_file_from_signed_uri(pack.signed_download_uri)
            return data[pack.offset : pack.offset + pack.size]
        case _:
            raise VolumeError(f"Unsupported file response type: {resp.content!r}")


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
            ref_name=self._volume._selector_ref(),
        )
        self._volume._update_version_from_statuses(statuses)


# ---------------------------------------------------------------------------
# Mount helpers
# ---------------------------------------------------------------------------


def normalize_volume_refs(
    volumes: Iterable[tuple[str | Volume, str] | Volume | VolumeMount] | None,
) -> builtins.list[VolumeMount]:
    """Validate and normalize caller-supplied volume references.

    Shared by every surface that accepts ``volumes=[...]`` so the accepted
    shape can't drift between sandboxes, containers, functions, classes, and
    scaling groups. Existence is checked later, at run/deploy time, by
    :func:`referenced_volume_mount`.
    """
    refs: builtins.list[VolumeMount] = []
    for item in volumes or []:
        if isinstance(item, Volume):
            refs.append(item.mount())
            continue
        if isinstance(item, VolumeMount):
            refs.append(item)
            continue
        if not (
            isinstance(item, (tuple, list))
            and len(item) == 2
            and isinstance(item[0], (str, Volume))
            and isinstance(item[1], str)
        ):
            raise ValueError(
                "Each volumes entry must be a Volume, VolumeMount, or "
                f"(name_or_volume, mount_path) tuple with a string mount path, got {item!r}. "
                "Example: volumes=[('my-volume', '/data')] or [(volume, '/data')]"
            )
        volume, mount_path = item
        if isinstance(volume, Volume):
            refs.append(volume.mount(mount_path))
        else:
            refs.append(VolumeMount(name=volume, mount_path=mount_path))
    return refs


def _require_volume_exists(connect: ConnectClient, name: str) -> None:
    """Raise :class:`VolumeError` unless a volume named ``name`` exists."""
    if not VolumeClient.from_connect(connect).exists(name):
        raise VolumeError(
            f"Referenced volume {name!r} was not found. "
            f"Check the name, or create it before referencing it."
        )


def referenced_volume_mount(connect: ConnectClient, mount: VolumeMount) -> VolumeMount:
    """Build a mount for a caller-referenced volume, failing fast if absent.

    A volume supplied as a ``(name, mount_path)`` tuple is owned by the caller
    and must already exist. Checking here turns a typo'd name into a deploy-time
    error instead of a container that mounts nothing.
    """
    if mount.version_id is None and mount.ref_name is None:
        _require_volume_exists(connect, mount.name)
    else:
        VolumeClient.from_connect(connect)._get_volume(
            mount.name,
            version_id=mount.version_id,
            ref_name=mount.ref_name,
        )
    return mount


def pinned_volume_mount(client: VolumeClient, mount: VolumeMount) -> VolumeMount:
    """Resolve a volume selector to an immutable version mount."""
    response = client._get_volume(
        mount.name,
        version_id=mount.version_id,
        ref_name=mount.ref_name,
    )
    if response.version is None:
        raise VolumeError(f"Volume {mount.name!r} has no committed version")
    return VolumeMount(
        mount.name,
        mount.mount_path,
        version_id=response.version.version_id,
    )


def resolve_referenced_volume_type(connect: ConnectClient, name: str) -> str:
    """Return the mount type of an existing volume, or raise if it is absent.

    Every volume mounts as :data:`MOUNT_TYPE`, so this no longer discriminates
    between backends. Prefer :func:`referenced_volume_mount`.
    """
    _require_volume_exists(connect, name)
    return MOUNT_TYPE


def is_workload_fork(mount: VolumeMount) -> bool:
    """Identify SDK-managed image forks."""
    return mount.name.startswith(_SHARED_SOURCE_VOLUME_PREFIX) and bool(
        mount.ref_name
        and mount.ref_name.startswith((_WORKLOAD_FORK_PREFIX, "cc-workload-"))
    )


def cleanup_owned_volumes(
    client: VolumeClient,
    all_volumes: Iterable[VolumeMount],
    referenced_volumes: Iterable[VolumeMount],
) -> None:
    """Delete owned forks and disposable volumes, retaining shared sources.

    ``all_volumes`` contains both volumes the SDK uploaded (e.g. for an
    image's lazy local files) and volumes the caller referenced via
    ``volumes=[(name, path)]``. Caller-referenced volumes are owned by
    the caller and must outlive this lifecycle.
    """
    referenced_volumes = list(referenced_volumes)
    referenced_names = {volume.name for volume in referenced_volumes}
    referenced_forks = {(volume.name, volume.ref_name) for volume in referenced_volumes}
    deleted_forks: set[tuple[str, str]] = set()
    for v in all_volumes:
        if is_workload_fork(v):
            assert v.ref_name is not None
            key = (v.name, v.ref_name)
            if key not in referenced_forks and key not in deleted_forks:
                try:
                    client._delete_ref(v.name, v.ref_name)
                except VolumeNotFoundError:
                    pass
                deleted_forks.add(key)
            continue
        if v.name in referenced_names or v.name.startswith(
            (_SHARED_SOURCE_VOLUME_PREFIX, "cc-local-", "cc-source-")
        ):
            continue
        client.delete_volume(v.name)
