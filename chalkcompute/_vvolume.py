"""Client utilities for Chalk versioned volumes."""

from __future__ import annotations

import builtins
import datetime
import logging
import os
import time
import uuid
from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import grpc
import httpx

from chalkcompute._connect import ConnectClient, grpc_target_from_url
from chalkcompute._gen.chalk.volume.v2 import volume_pb2 as pb_v2
from chalkcompute._gen.chalk.volume.v2 import volume_pb2_grpc as pb_v2_grpc
from chalkcompute._image import Image
from chalkcompute._tracing import traced
from chalkcompute._volume import (
    FileInfo,
    VolumeClosedError,
    VolumeError,
    VolumeInfo,
    _GrpcMetadata,
    _GrpcOptions,
    _handle_rpc_error,
    _read_file_from_signed_uri,
)

logger = logging.getLogger(__name__)

# Versioned volume writer defaults match fuse-driver's VersionedWriterConfig.
_VERSIONED_INLINE_THRESHOLD_BYTES: int = 4 * 1024
_VERSIONED_CHUNK_SIZE_BYTES: int = 4 * 1024 * 1024
_VERSIONED_COMMIT_MAX_REBASE_RETRIES: int = 3
_VERSIONED_COMMIT_POLL_INTERVAL_SECONDS: float = 0.5
_VERSIONED_COMMIT_POLL_TIMEOUT_SECONDS: float = 30.0
_VERSIONED_TRANSFER_MAX_WORKERS: int = 32
_VERSIONED_BATCH_MAX_FILES: int = 200


@dataclass
class VersionInfo:
    """Metadata for an immutable versioned volume snapshot.

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
    index_id
        Server-side content index identifier, if exposed.
    """

    id: int
    sequence_number: int = 0
    parent_id: int | None = None
    created_at: datetime.datetime | None = None
    ref: str | None = None
    index_id: str | None = None

    @property
    def version_id(self) -> int:
        """Alias for the protobuf ``version_id`` field name."""
        return self.id


@dataclass
class CommitStatus:
    """Result of a versioned volume commit attempt.

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


class VersionedVolume:
    """A handle to a versioned volume using the v2 volume service.

    Versioned volumes expose immutable snapshots behind mutable refs. The
    Python API supports direct file writes, reads, listing, and version
    lookup. Each write produces a new immutable version; the ``main`` ref
    (or a caller-provided ref) advances to point at the latest committed
    version.

    Examples
    --------
    Write a file, advance the ref, and read the result back:

    >>> from chalkcompute import VersionedVolume
    >>> with VersionedVolume("models") as vol:
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
        client: VersionedVolumeClient | None = None,
        *,
        version_id: int | None = None,
        ref: str = "main",
        create_if_missing: bool = True,
    ) -> None:
        """Initialize a versioned volume handle.

        Parameters
        ----------
        name
            Volume name.
        client
            Optional :class:`VersionedVolumeClient`. If ``None``, one is
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
            raise VolumeError(f"Versioned volume {self._name!r} has no version")
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
            resp = self._client._stub.GetVolume(
                pb_v2.GetVolumeRequest(
                    volume=self._volume_ref(),
                    selector=self._selector(),
                ),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            code = getattr(e, "code", lambda: None)()
            if not self._create_if_missing or code != grpc.StatusCode.NOT_FOUND:
                _handle_rpc_error(e, self._name)
                raise
        else:
            version = (
                _pb_v2_to_version_info(resp.version)
                if resp.HasField("version")
                else None
            )
            return _pb_v2_to_volume_info(resp.volume), version

        try:
            resp = self._client._stub.CreateVolume(
                pb_v2.CreateVolumeRequest(name=self._name),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._name)
            raise
        return _pb_v2_to_volume_info(resp.volume), None

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise VolumeClosedError(f"VersionedVolume {self._name!r} is closed")

    def _ensure_ready(self) -> None:
        self._raise_if_closed()
        if self._info is None:
            self._info, self._version = self._fetch_info()

    @traced("chalkcompute.versioned_volume.read_file")
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
        try:
            resp = self._client._stub.GetFile(
                pb_v2.GetFileRequest(
                    volume=self._volume_ref(),
                    path=path,
                    selector=self._selector(),
                ),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._name)
            raise
        data = _read_versioned_file_response(resp)
        logger.info(
            "Read %s from versioned volume %s (%s, %d bytes)",
            path,
            self._name,
            resp.WhichOneof("content"),
            len(data),
        )
        return data

    @traced("chalkcompute.versioned_volume.put_file")
    def put_file(self, path: str, data: bytes | str) -> VersionedVolume:
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
        VersionedVolume
            ``self``, to allow chaining.
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._commit_files([(path, data)])
        return self

    def put_file_from_path(self, path: str, local_path: str | Path) -> VersionedVolume:
        """Upload a local file into the volume.

        Parameters
        ----------
        path
            Destination path within the volume.
        local_path
            Local file to read and upload.

        Returns
        -------
        VersionedVolume
            ``self``, to allow chaining.
        """
        self.put_file(path, Path(local_path).read_bytes())
        return self

    def remove_file(self, path: str) -> VersionedVolume:
        """Remove a file, committing a new version on the current ref.

        Parameters
        ----------
        path
            File path within the volume.

        Returns
        -------
        VersionedVolume
            ``self``, to allow chaining.
        """
        self._commit_removes([path])
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
            logger.info(
                "Listed %d entries in versioned volume %s under %r",
                len(files),
                self._name,
                prefix,
            )
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
        logger.info(
            "Listed %d entries in versioned volume %s under %r",
            len(out),
            self._name,
            prefix,
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
        max_files_per_commit: int = _VERSIONED_BATCH_MAX_FILES,
    ) -> Generator[_VersionedBatchUpload, None, None]:
        """Context manager that commits staged writes as one version.

        Files staged within the block are bundled into a single commit
        when the block exits (or when ``max_files_per_commit`` is reached).

        Parameters
        ----------
        max_files_per_commit
            Maximum files per commit before flushing automatically.

        Yields
        ------
        _VersionedBatchUpload
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
        batch = _VersionedBatchUpload(
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
            try:
                resp = self._client._stub.ListVolumeVersions(
                    pb_v2.ListVolumeVersionsRequest(
                        volume=self._volume_ref(),
                        limit=limit,
                        cursor=cursor,
                    ),
                    metadata=self._client._metadata,
                )
            except grpc.RpcError as e:
                _handle_rpc_error(e, self._name)
                raise
            versions.extend(_pb_v2_to_version_info(v) for v in resp.versions)
            cursor = resp.next_cursor
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
        try:
            resp = self._client._stub.GetVolume(
                pb_v2.GetVolumeRequest(
                    volume=self._volume_ref(),
                    selector=pb_v2.VersionSelector(ref=self._ref),
                ),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._name)
            raise
        self._info = _pb_v2_to_volume_info(resp.volume)
        if not resp.HasField("version"):
            raise VolumeError(f"Versioned volume {self._name!r} has no latest version")
        self._version = _pb_v2_to_version_info(resp.version)
        logger.info(
            "Fetched latest version for versioned volume %s: version=%d sequence=%d",
            self._name,
            self._version.id,
            self._version.sequence_number,
        )
        return self._version

    def at_version(self, version_id: int) -> VersionedVolume:
        """Return a read-only handle pinned to a specific version.

        Mutating operations on the returned handle raise
        :class:`VolumeError`.

        Parameters
        ----------
        version_id
            The immutable version to pin to.

        Returns
        -------
        VersionedVolume
            A new handle pointing at ``version_id``.
        """
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
        """Delete this volume and all of its versions.

        Raises
        ------
        VolumeError
            If called on a version-pinned (read-only) handle.
        """
        self._ensure_mutable()
        self._ensure_ready()
        try:
            self._client._stub.DeleteVolume(
                pb_v2.DeleteVolumeRequest(volume=self._volume_ref()),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._name)
            raise
        logger.info("Deleted versioned volume %s", self._name)

    def _commit_files(self, files: list[tuple[str, bytes]]) -> VersionInfo:
        self._ensure_mutable()
        self._ensure_ready()
        upserts: list[pb_v2.PathFileDelta] = []
        uploaded: list[pb_v2.UploadedObjectReference] = []
        total_bytes = 0
        for path, data in files:
            total_bytes += len(data)
            node, refs = self._file_node_for_data(data)
            upserts.append(
                pb_v2.PathFileDelta(
                    path=path,
                    node=node,
                    mode=pb_v2.PATH_WRITE_MODE_UPSERT,
                )
            )
            uploaded.extend(refs)
        logger.info(
            "Prepared %d file upserts for versioned volume %s (%d bytes, %d uploaded objects)",
            len(upserts),
            self._name,
            total_bytes,
            len(uploaded),
        )
        return self._commit_path_deltas(upserts=upserts, removes=[], uploaded=uploaded)

    def _commit_removes(self, paths: list[str]) -> VersionInfo:
        self._ensure_mutable()
        self._ensure_ready()
        removes = [pb_v2.PathRemoveDelta(path=path, recursive=False) for path in paths]
        logger.info(
            "Prepared %d removes for versioned volume %s",
            len(removes),
            self._name,
        )
        return self._commit_path_deltas(upserts=[], removes=removes, uploaded=[])

    def _commit_path_deltas(
        self,
        *,
        upserts: list[pb_v2.PathFileDelta],
        removes: list[pb_v2.PathRemoveDelta],
        uploaded: list[pb_v2.UploadedObjectReference],
    ) -> VersionInfo:
        last_status: CommitStatus | None = None
        for attempt in range(_VERSIONED_COMMIT_MAX_REBASE_RETRIES + 1):
            base = self.latest_version()
            commit_id = str(uuid.uuid4())
            intent = pb_v2.CommitIntent(
                volume=self._volume_ref(),
                commit_id=commit_id,
                ref=self._ref,
                base_version_id=base.id,
                base_sequence_number=base.sequence_number,
                uploaded_object_references=uploaded,
                author="chalkcompute",
                path_deltas=pb_v2.PathDeltaList(upserts=upserts, removes=removes),
            )
            logger.info(
                "Committing versioned volume %s commit=%s base=%d upserts=%d removes=%d uploaded=%d attempt=%d",
                self._name,
                commit_id,
                base.id,
                len(upserts),
                len(removes),
                len(uploaded),
                attempt + 1,
            )
            try:
                resp = self._client._stub.CommitVersion(
                    pb_v2.CommitVersionRequest(intent=intent),
                    metadata=self._client._metadata,
                )
            except grpc.RpcError as e:
                _handle_rpc_error(e, self._name)
                raise
            status = self._wait_for_commit_status(resp.status)
            last_status = status
            if status.result == "committed" and status.version is not None:
                self._version = status.version
                logger.info(
                    "Committed versioned volume %s commit=%s version=%d sequence=%d",
                    self._name,
                    commit_id,
                    status.version.id,
                    status.version.sequence_number,
                )
                return status.version
            if status.result != "rebase_required":
                raise VolumeError(
                    f"Versioned volume commit {commit_id} returned {status.result}"
                )
            latest = status.latest_version.id if status.latest_version else None
            logger.warning(
                "Versioned volume %s commit=%s requires rebase (latest=%s)",
                self._name,
                commit_id,
                latest,
            )
        latest = last_status.latest_version if last_status else None
        latest_msg = f"; latest version is {latest.id}" if latest else ""
        raise VolumeError(f"Versioned volume commit requires rebase{latest_msg}")

    def _wait_for_commit_status(self, status_pb: pb_v2.CommitStatus) -> CommitStatus:
        status = _pb_v2_to_commit_status(status_pb)
        deadline = time.monotonic() + _VERSIONED_COMMIT_POLL_TIMEOUT_SECONDS
        poll_count = 0
        while status.result == "pending" and time.monotonic() < deadline:
            time.sleep(_VERSIONED_COMMIT_POLL_INTERVAL_SECONDS)
            poll_count += 1
            try:
                resp = self._client._stub.GetCommitStatus(
                    pb_v2.GetCommitStatusRequest(
                        volume=self._volume_ref(),
                        commit_id=status.commit_id,
                    ),
                    metadata=self._client._metadata,
                )
            except grpc.RpcError as e:
                _handle_rpc_error(e, self._name)
                raise
            status = _pb_v2_to_commit_status(resp.status)
        if status.result == "pending":
            raise VolumeError(
                f"Timed out waiting for versioned volume commit {status.commit_id}"
            )
        if poll_count:
            logger.info(
                "Versioned volume commit %s reached %s after %d polls",
                status.commit_id,
                status.result,
                poll_count,
            )
        return status

    def _file_node_for_data(
        self, data: bytes
    ) -> tuple[pb_v2.FileNode, list[pb_v2.UploadedObjectReference]]:
        metadata = pb_v2.FileMetadata(mode=0o644)
        _set_timestamp_now(metadata.updated_at)
        if not data:
            content = pb_v2.ContentRef(empty=pb_v2.EmptyFileContent())
            logger.info("Prepared empty versioned file node")
            return (
                pb_v2.FileNode(
                    metadata=metadata,
                    file=pb_v2.RegularFileNode(content=content),
                ),
                [],
            )
        if len(data) <= _VERSIONED_INLINE_THRESHOLD_BYTES:
            content = pb_v2.ContentRef(
                inline=pb_v2.InlineFileContent(
                    data=data,
                    hash=_blake3_hex(data),
                    size=len(data),
                )
            )
            logger.info("Prepared inline versioned file node (%d bytes)", len(data))
            return (
                pb_v2.FileNode(
                    metadata=metadata,
                    file=pb_v2.RegularFileNode(content=content),
                ),
                [],
            )
        chunk_refs, uploaded_refs = self._upload_chunked_content(data)
        content = pb_v2.ContentRef(
            chunked=pb_v2.ChunkedContentRef(
                hash=_blake3_hex(data),
                size=len(data),
                chunks=chunk_refs,
            )
        )
        logger.info(
            "Prepared chunked versioned file node (%d bytes, %d chunks)",
            len(data),
            len(chunk_refs),
        )
        return (
            pb_v2.FileNode(
                metadata=metadata,
                file=pb_v2.RegularFileNode(content=content),
            ),
            uploaded_refs,
        )

    def _upload_chunked_content(
        self, data: bytes
    ) -> tuple[list[pb_v2.ChunkRef], list[pb_v2.UploadedObjectReference]]:
        chunks: list[tuple[str, str, int, int, bytes]] = []
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + _VERSIONED_CHUNK_SIZE_BYTES]
            chunk_hash = _blake3_hex(chunk)
            relative_key = f"{chunk_hash[:2]}/{chunk_hash}"
            chunks.append((relative_key, chunk_hash, len(chunk), offset, chunk))
            offset += len(chunk)
        logger.info(
            "Requesting upload URLs for versioned file content (%d bytes, %d chunks)",
            len(data),
            len(chunks),
        )

        requested = [
            pb_v2.UploadedObjectReference(
                object_key=relative_key,
                hash=chunk_hash,
                content_size=size,
                kind=pb_v2.UPLOADED_OBJECT_KIND_CHUNK,
            )
            for relative_key, chunk_hash, size, _offset, _chunk in chunks
        ]
        try:
            resp = self._client._stub.RequestUploadURLs(
                pb_v2.RequestUploadURLsRequest(
                    volume=self._volume_ref(),
                    objects=requested,
                ),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._name)
            raise
        if len(resp.urls) != len(chunks):
            raise VolumeError(
                "Versioned volume upload URL response length did not match chunks"
            )

        chunk_refs: list[pb_v2.ChunkRef] = []
        uploaded_refs: list[pb_v2.UploadedObjectReference] = []
        upload_jobs: list[tuple[str, bytes]] = []
        uploaded_count = 0
        reused_count = 0
        for item, (_relative_key, chunk_hash, size, offset, chunk) in zip(
            resp.urls, chunks, strict=True
        ):
            if not item.object_key:
                raise VolumeError("Versioned volume upload URL item missing object_key")
            if not item.already_exists:
                if not item.signed_upload_uri:
                    raise VolumeError(
                        f"Versioned volume upload URL missing for {item.object_key!r}"
                    )
                upload_jobs.append((item.signed_upload_uri, chunk))
                uploaded_count += 1
            else:
                reused_count += 1
            chunk_refs.append(
                pb_v2.ChunkRef(
                    object_key=item.object_key,
                    hash=chunk_hash,
                    size=size,
                    offset=offset,
                )
            )
            uploaded_refs.append(
                pb_v2.UploadedObjectReference(
                    object_key=item.object_key,
                    hash=chunk_hash,
                    content_size=size,
                    kind=pb_v2.UPLOADED_OBJECT_KIND_CHUNK,
                )
            )
        if upload_jobs:
            with ThreadPoolExecutor(
                max_workers=_transfer_worker_count(len(upload_jobs))
            ) as pool:
                list(pool.map(_put_versioned_chunk_job, upload_jobs))
        logger.info(
            "Uploaded versioned chunks (%d uploaded, %d reused)",
            uploaded_count,
            reused_count,
        )
        return chunk_refs, uploaded_refs

    def _listdir_page_all(self, path: str) -> list[FileInfo]:
        files: list[FileInfo] = []
        cursor = ""
        while True:
            try:
                resp = self._client._stub.ListFiles(
                    pb_v2.ListFilesRequest(
                        volume=self._volume_ref(),
                        path=path,
                        recursive=False,
                        limit=1000,
                        cursor=cursor,
                        selector=self._selector(),
                    ),
                    metadata=self._client._metadata,
                )
            except grpc.RpcError as e:
                _handle_rpc_error(e, self._name)
                raise
            files.extend(_pb_v2_to_file_info(f) for f in resp.files)
            cursor = resp.next_cursor
            if not cursor:
                return files

    def _volume_ref(self) -> pb_v2.VolumeRef:
        volume_id = self._info.volume_id if self._info is not None else None
        return pb_v2.VolumeRef(volume_id=volume_id or "", name=self._name)

    def _selector(self) -> pb_v2.VersionSelector:
        if self._version_id is not None:
            return pb_v2.VersionSelector(version_id=self._version_id)
        if self._ref and self._ref != "main":
            return pb_v2.VersionSelector(ref=self._ref)
        return pb_v2.VersionSelector()

    def _ensure_mutable(self) -> None:
        self._raise_if_closed()
        if self._version_id is not None:
            raise VolumeError("VersionedVolume.at_version(...) handles are read-only")

    def close(self) -> None:
        """Close the underlying client used by this VersionedVolume.

        Safe to call multiple times.
        """
        if not self._closed:
            self._client.close()
        self._closed = True

    def __enter__(self) -> VersionedVolume:
        """Enter a context that closes the volume on exit."""
        self._raise_if_closed()
        return self

    def __exit__(self, *_args: object) -> None:
        """Close the volume on context exit."""
        self.close()

    def __repr__(self) -> str:
        if self._version_id is not None:
            return (
                f"VersionedVolume(name={self._name!r}, version_id={self._version_id!r})"
            )
        return f"VersionedVolume(name={self._name!r}, ref={self._ref!r})"


class VersionedVolumeClient:
    """Client for the v2 versioned Volume gRPC service.

    Satisfies the :class:`VolumeDriver` protocol with
    ``mount_type = "versioned_chalkfs"``, so container/sandbox lazy-file
    mounts go through the versioned backend by default.
    """

    mount_type = "versioned_chalkfs"

    def __init__(
        self,
        target: str = "localhost:50051",
        *,
        credentials: grpc.ChannelCredentials | None = None,
        metadata: _GrpcMetadata | None = None,
        options: _GrpcOptions | None = None,
    ) -> None:
        """Initialize the gRPC client.

        Parameters
        ----------
        target
            gRPC server address (e.g. ``"localhost:50051"``).
        credentials
            Optional TLS/SSL channel credentials. If ``None``, uses an
            insecure channel.
        metadata
            Optional default metadata sent with every RPC.
        options
            Optional gRPC channel options.
        """
        self._target = target
        self._metadata = tuple(metadata) if metadata else ()
        if credentials is not None:
            self._channel = grpc.secure_channel(target, credentials, options=options)
        else:
            self._channel = grpc.insecure_channel(target, options=options)
        self._stub = pb_v2_grpc.VolumeServiceStub(self._channel)

    @classmethod
    def from_connect(cls, connect: ConnectClient) -> VersionedVolumeClient:
        """Create a client from an authenticated :class:`ConnectClient`.

        Parameters
        ----------
        connect
            An authenticated ``ConnectClient``. Auth is ensured
            automatically if not already done.

        Returns
        -------
        VersionedVolumeClient
            A new client with TLS credentials and metadata derived from the
            ``ConnectClient``'s token and environment.
        """
        connect._ensure_auth()
        host, use_tls = grpc_target_from_url(connect._api_server or "")
        return cls(
            host,
            credentials=grpc.ssl_channel_credentials() if use_tls else None,
            metadata=[
                ("authorization", f"Bearer {connect._access_token}"),
                ("x-chalk-env-id", connect._env_id or ""),
                ("x-chalk-server", "go-api"),
            ],
            options=[
                ("grpc.max_send_message_length", -1),
                ("grpc.max_receive_message_length", -1),
            ],
        )

    def __enter__(self) -> VersionedVolumeClient:
        """Enter a context that closes the channel on exit."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Close the gRPC channel on context exit."""
        self.close()

    def close(self) -> None:
        """Close the underlying gRPC channel."""
        self._channel.close()

    @traced("chalkcompute.versioned_volume.create")
    def create(self, name: str) -> VersionedVolume:
        """Create a new named versioned volume.

        Parameters
        ----------
        name
            Name for the new volume.

        Returns
        -------
        VersionedVolume
            A handle to the newly created volume.
        """
        try:
            resp = self._stub.CreateVolume(
                pb_v2.CreateVolumeRequest(name=name),
                metadata=self._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, name)
            raise
        vol = VersionedVolume(name, client=self, create_if_missing=False)
        vol._info = _pb_v2_to_volume_info(resp.volume)
        return vol

    def from_name(self, name: str) -> VersionedVolume:
        """Return a handle by name without validating that it exists.

        Parameters
        ----------
        name
            Name of the existing volume.

        Returns
        -------
        VersionedVolume
            A handle that fetches metadata lazily on first use.
        """
        return VersionedVolume(name, client=self, create_if_missing=False)

    def lookup(self, name: str) -> VersionedVolume:
        """Look up a volume by name, verifying it exists.

        Parameters
        ----------
        name
            Name of the volume.

        Returns
        -------
        VersionedVolume
            A handle with metadata pre-fetched.

        Raises
        ------
        VolumeNotFoundError
            If the volume does not exist.
        """
        vol = VersionedVolume(name, client=self, create_if_missing=False)
        vol.reload()
        return vol

    @traced("chalkcompute.versioned_volume.upload_lazy_files")
    def upload_lazy_files(
        self, image: Image, *, volume_prefix: str = "files"
    ) -> builtins.list[tuple[str, str]]:
        """Upload an image's lazy local files into versioned volumes.

        Creates one versioned volume per destination directory and commits
        all files for that directory as a single version.

        Parameters
        ----------
        image
            Image whose lazy local files should be uploaded.
        volume_prefix
            Prefix used when naming the generated volumes.

        Returns
        -------
        list of tuple of (str, str)
            ``(volume_name, mount_path)`` pairs ready to mount with
            ``type="versioned_chalkfs"`` in a container spec.
        """
        try:
            dir_to_files = image.lazy_local_files_grouped_for_volume_mounts()
        except ValueError as e:
            raise VolumeError(str(e)) from e

        volumes: builtins.list[tuple[str, str]] = []
        for dest_dir, files in dir_to_files.items():
            vol = self.create(f"{volume_prefix}-{uuid.uuid4().hex[:12]}")
            with vol.batch_upload() as batch:
                for lf in files:
                    batch.put_file(os.path.relpath(lf.dest, dest_dir), lf.src)
            volumes.append((vol.name, dest_dir))
        return volumes

    def delete_volume(self, name: str) -> None:
        """Delete a versioned volume by name.

        Parameters
        ----------
        name
            Volume to delete.
        """
        try:
            self._stub.DeleteVolume(
                pb_v2.DeleteVolumeRequest(volume=pb_v2.VolumeRef(name=name)),
                metadata=self._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, name)
            raise

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
            resp = self._stub.ListVolumes(
                pb_v2.ListVolumesRequest(limit=limit, cursor=cursor),
                metadata=self._metadata,
            )
            volumes.extend(_pb_v2_listed_to_volume_info(v) for v in resp.volumes)
            cursor = resp.next_cursor
            if not cursor:
                return volumes


def _pb_v2_to_volume_info(info: pb_v2.VolumeInfo) -> VolumeInfo:
    created_at = None
    if info.HasField("created_at"):
        created_at = info.created_at.ToDatetime(tzinfo=datetime.timezone.utc)
    return VolumeInfo(
        name=info.name,
        created_at=created_at,
        volume_id=info.volume_id or None,
        access_mode=_volume_access_mode_name(info.access_mode),
        ref=info.ref or None,
    )


def _pb_v2_listed_to_volume_info(info: pb_v2.ListedVolume) -> VolumeInfo:
    created_at = None
    if info.HasField("created_at"):
        created_at = info.created_at.ToDatetime(tzinfo=datetime.timezone.utc)
    return VolumeInfo(name=info.name, created_at=created_at)


def _pb_v2_to_version_info(info: pb_v2.VersionInfo) -> VersionInfo:
    created_at = None
    if info.HasField("created_at"):
        created_at = info.created_at.ToDatetime(tzinfo=datetime.timezone.utc)
    parent_id = info.parent_id if info.HasField("parent_id") else None
    return VersionInfo(
        id=info.version_id,
        sequence_number=info.sequence_number,
        parent_id=parent_id,
        created_at=created_at,
        ref=info.ref or None,
        index_id=info.index_id or None,
    )


def _pb_v2_to_file_info(info: pb_v2.FileInfo) -> FileInfo:
    updated_at = None
    if info.HasField("updated_at"):
        updated_at = info.updated_at.ToDatetime(tzinfo=datetime.timezone.utc)
    return FileInfo(
        path=info.path,
        size=info.size,
        updated_at=updated_at,
        kind=_file_kind_name(info.kind),
        hash=info.hash or None,
        mode=info.mode if info.HasField("mode") else None,
        uid=info.uid,
        gid=info.gid,
        nlink=info.nlink,
    )


def _pb_v2_to_commit_status(status: pb_v2.CommitStatus) -> CommitStatus:
    created_at = None
    if status.HasField("created_at"):
        created_at = status.created_at.ToDatetime(tzinfo=datetime.timezone.utc)
    version = (
        _pb_v2_to_version_info(status.version) if status.HasField("version") else None
    )
    latest_version = (
        _pb_v2_to_version_info(status.latest_version)
        if status.HasField("latest_version")
        else None
    )
    return CommitStatus(
        commit_id=status.commit_id,
        result=_commit_result_name(status.result),
        version=version,
        latest_version=latest_version,
        created_at=created_at,
    )


def _read_versioned_file_response(resp: pb_v2.GetFileResponse) -> bytes:
    content_type = resp.WhichOneof("content")
    if content_type == "data":
        return resp.data
    if content_type == "chunked":
        chunks = sorted(resp.chunked.chunks, key=lambda c: c.offset)
        if not chunks:
            return b""
        with ThreadPoolExecutor(
            max_workers=_transfer_worker_count(len(chunks))
        ) as pool:
            parts = list(pool.map(_read_versioned_chunk, chunks))
        return b"".join(parts)
    if content_type == "packed":
        pack = resp.packed.pack
        return _read_range_from_signed_uri(
            pack.signed_download_uri,
            pack.offset,
            pack.size,
        )
    raise VolumeError(f"Unsupported versioned file response type: {content_type!r}")


def _read_range_from_signed_uri(
    signed_download_uri: str, offset: int, size: int
) -> bytes:
    headers = {"Range": f"bytes={offset}-{offset + size - 1}"}
    try:
        resp = httpx.get(signed_download_uri, headers=headers, timeout=300)
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


def _transfer_worker_count(item_count: int) -> int:
    return max(1, min(_VERSIONED_TRANSFER_MAX_WORKERS, item_count))


def _read_versioned_chunk(chunk: pb_v2.SignedChunkRef) -> bytes:
    return _read_file_from_signed_uri(chunk.signed_download_uri)


def _put_versioned_chunk_job(job: tuple[str, bytes]) -> None:
    signed_upload_uri, data = job
    _put_versioned_chunk(signed_upload_uri, data)


def _put_versioned_chunk(signed_upload_uri: str, data: bytes) -> None:
    try:
        resp = httpx.put(
            signed_upload_uri,
            content=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=600,
        )
        resp.raise_for_status()
    except httpx.TimeoutException as e:
        raise VolumeError(f"Timed out uploading signed chunk URL: {e}") from e
    except httpx.HTTPStatusError as e:
        raise VolumeError(
            f"Signed chunk upload failed with status {e.response.status_code}"
        ) from e
    except httpx.HTTPError as e:
        raise VolumeError(f"Signed chunk upload failed: {e}") from e


def _blake3_hex(data: bytes) -> str:
    try:
        import blake3
    except ModuleNotFoundError as e:
        raise VolumeError(
            "VersionedVolume writes require the 'blake3' package. "
            "Install chalkcompute with its declared dependencies."
        ) from e
    return blake3.blake3(data).hexdigest()


def _set_timestamp_now(ts: object) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    setattr(ts, "seconds", int(now.timestamp()))
    setattr(ts, "nanos", now.microsecond * 1000)


def _volume_access_mode_name(mode: int) -> str:
    if mode == pb_v2.VOLUME_ACCESS_MODE_READ_ONLY:
        return "read_only"
    if mode == pb_v2.VOLUME_ACCESS_MODE_READ_WRITE:
        return "read_write"
    return "unspecified"


def _file_kind_name(kind: int) -> str:
    if kind == pb_v2.FILE_KIND_FILE:
        return "file"
    if kind == pb_v2.FILE_KIND_DIRECTORY:
        return "directory"
    if kind == pb_v2.FILE_KIND_SYMLINK:
        return "symlink"
    return "unspecified"


def _commit_result_name(result: int) -> str:
    if result == pb_v2.COMMIT_RESULT_COMMITTED:
        return "committed"
    if result == pb_v2.COMMIT_RESULT_PENDING:
        return "pending"
    if result == pb_v2.COMMIT_RESULT_REBASE_REQUIRED:
        return "rebase_required"
    if result == pb_v2.COMMIT_RESULT_ABORTED:
        return "aborted"
    return "unspecified"


class _VersionedBatchUpload:
    """Accumulates versioned volume writes and commits them as one version."""

    def __init__(
        self,
        volume: VersionedVolume,
        *,
        max_files_per_commit: int,
    ) -> None:
        self._volume = volume
        self._max_files_per_commit = max_files_per_commit
        self._files: list[tuple[str, bytes]] = []

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
        self._files.append((path, data))
        self._flush_if_needed()

    def put_file(self, path: str, local_path: str | Path) -> None:
        """Stage a local file for the next commit.

        Parameters
        ----------
        path
            Destination path within the volume.
        local_path
            Local file to read.
        """
        self._files.append((path, Path(local_path).read_bytes()))
        self._flush_if_needed()

    def _flush_if_needed(self) -> None:
        if len(self._files) >= self._max_files_per_commit:
            self._flush()

    def _flush(self) -> VersionInfo | None:
        if not self._files:
            return None
        files = self._files
        self._files = []
        logger.info(
            "Flushing versioned batch upload for %s (%d files)",
            self._volume.name,
            len(files),
        )
        try:
            return self._volume._commit_files(files)
        except Exception:
            self._files = files + self._files
            raise


