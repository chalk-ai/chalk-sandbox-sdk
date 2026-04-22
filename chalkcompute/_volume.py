"""
Python SDK for the Volume gRPC Service.

Provides an API for managing persistent named volumes backed by
object storage.

Usage:
    from chalkcompute import VolumeClient

    with VolumeClient("localhost:50051") as client:
        # Create or look up a volume
        vol = client.create("my-data")

        # Upload files
        vol.put_file("models/latest.bin", model_bytes)
        vol.put_file("config.json", b'{"key": "value"}')

        # Batch upload for efficiency
        with vol.batch_upload() as batch:
            batch.put("data/a.csv", csv_a)
            batch.put("data/b.csv", csv_b)
            batch.put("data/c.csv", csv_c)

        # Read files
        data = vol.read_file("models/latest.bin")

        # List files
        for f in vol.listdir("data/"):
            print(f"{f.path}  {f.size} bytes")

        # Remove a file
        vol.remove_file("config.json")

        # Delete the volume entirely
        vol.delete()
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import grpc

from chalkcompute._connect import ConnectClient, grpc_target_from_url, raise_for_grpc_error
from chalkcompute._gen.chalk.volume.v1 import volume_pb2 as pb
from chalkcompute._gen.chalk.volume.v1 import volume_pb2_grpc as pb_grpc
from chalkcompute._image import Image, LazyLocalFile
from chalkcompute._tracing import traced

# Aliases defined before VolumeClient, which has a ``.list()`` method that
# would otherwise shadow the builtin ``list`` in class-scope name resolution.
_GrpcMetadata = list[tuple[str, str]]
_GrpcOptions = list[tuple[str, int]]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Upload routing constants
# ---------------------------------------------------------------------------

#: Files at or below this size are uploaded via gRPC inline PutFile(data=...).
#: Files above this threshold use presigned object-storage upload.
_INLINE_THRESHOLD_BYTES: int = 8 * 1024 * 1024  # 8 MB

#: Part size sent to GetObjectUploadUri for multipart uploads.
_PRESIGNED_PART_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB

#: Maximum number of signed-URL refreshes before giving up on a presigned upload.
_PRESIGNED_MAX_RETRIES: int = 3

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VolumeError(Exception):
    """Base exception for volume errors."""


class VolumeNotFoundError(VolumeError):
    """Raised when a volume is not found."""


class VolumeFileNotFoundError(VolumeError):
    """Raised when a file within a volume is not found."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class VolumeInfo:
    """Metadata about a volume."""

    name: str
    """Name of the volume."""

    created_at: datetime.datetime | None = None
    """Timestamp when the volume was created."""


@dataclass
class FileInfo:
    """Metadata about a file inside a volume."""

    path: str
    """File path within the volume."""

    size: int
    """File size in bytes."""

    updated_at: datetime.datetime | None = None
    """Timestamp when the file was last updated."""


# ---------------------------------------------------------------------------
# Batch upload context manager
# ---------------------------------------------------------------------------


class _BatchUpload:
    """Accumulates put_file operations and executes them on exit."""

    def __init__(self, volume: Volume) -> None:
        self._volume = volume
        self._files: list[tuple[str, bytes]] = []

    def put(self, path: str, data: bytes | str) -> None:
        """Stage a file for upload.

        Parameters
        ----------
        path
            Destination path within the volume.
        data
            File contents (bytes or str).
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._files.append((path, data))

    def put_file(self, path: str, local_path: str | Path) -> None:
        """Stage a local file for upload.

        Parameters
        ----------
        path
            Destination path within the volume.
        local_path
            Local file to read.
        """
        self._files.append((path, Path(local_path).read_bytes()))

    def _flush(self) -> None:
        for path, data in self._files:
            self._volume.put_file(path, data)
        self._files.clear()


# ---------------------------------------------------------------------------
# Volume handle
# ---------------------------------------------------------------------------


class Volume:
    """A handle to a named volume, providing file operations.

    Volumes are persistent storage backed by object storage. Obtain an
    instance via :meth:`VolumeClient.create` or :meth:`VolumeClient.get`.

    Examples
    --------
    Create a volume, upload files, and read them back:

    >>> from chalkcompute import VolumeClient
    >>> client = VolumeClient(target="volumes.example.com:443")
    >>> vol = client.create("models")
    >>> vol.put("config.yaml", b"learning_rate: 0.01\\n")
    >>> vol.get("config.yaml")
    b'learning_rate: 0.01\\n'
    >>> [f.path for f in vol.list()]
    ['config.yaml']

    Batch-upload multiple files atomically:

    >>> with vol.batch_upload() as batch:
    ...     batch.put("a.txt", b"hello")
    ...     batch.put("b.txt", b"world")
    """

    def __init__(self, name: str, client: VolumeClient | None = None) -> None:
        self._client = client or VolumeClient.from_connect(ConnectClient())
        self._name = name
        self._info: VolumeInfo | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def info(self) -> VolumeInfo:
        """Get volume info, fetching from server if needed."""
        if self._info is None:
            self._info = self._fetch_info()
        return self._info

    def reload(self) -> VolumeInfo:
        """Force-refresh volume info from the server."""
        self._info = self._fetch_info()
        return self._info

    def _fetch_info(self) -> VolumeInfo:
        try:
            resp = self._client._stub.GetVolume(
                pb.GetVolumeRequest(name=self._name),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._name)
            raise
        return _pb_to_volume_info(resp.volume)

    # -- File operations -------------------------------------------------------

    @traced("chalkcompute.volume.read_file")
    def read_file(self, path: str) -> bytes:
        """Read a file's contents from the volume.

        Parameters
        ----------
        path
            File path within the volume.

        Returns
        -------
        bytes
            The raw file bytes.
        """
        try:
            resp = self._client._stub.GetFile(
                pb.GetFileRequest(volume_name=self._name, path=path),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._name)
            raise
        return resp.data

    @traced("chalkcompute.volume.put_file")
    def put_file(self, path: str, data: bytes | str) -> Volume:
        """Write a file into the volume.

        Small files (≤ 8 MB) are uploaded via gRPC inline ``PutFile(data=...)``.
        Large files use a presigned object-storage URL (``GetObjectUploadUri`` →
        direct S3/GCS upload → ``PutFile(storageObjectId=...)``).

        Parameters
        ----------
        path
            Destination path within the volume.
        data
            File contents (bytes or str).

        Returns
        -------
        Volume
            ``self``, to allow chaining.
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        _upload_bytes(self._client, self._name, path, data)
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
        self.put_file(path, Path(local_path).read_bytes())
        return self

    def remove_file(self, path: str) -> Volume:
        """Remove a file from the volume.

        Parameters
        ----------
        path
            File path within the volume.

        Returns
        -------
        Volume
            ``self``, to allow chaining.
        """
        try:
            self._client._stub.RemoveFile(
                pb.RemoveFileRequest(volume_name=self._name, path=path),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._name)
            raise
        return self

    def listdir(self, prefix: str = "", *, recursive: bool = True) -> list[FileInfo]:
        """List files in the volume.

        Parameters
        ----------
        prefix
            Only return files whose paths start with this prefix.
        recursive
            Currently always recursive (server-side behavior).

        Returns
        -------
        list of FileInfo
            One entry per matching file.
        """
        try:
            resp = self._client._stub.ListFiles(
                pb.ListFilesRequest(volume_name=self._name, prefix=prefix),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._name)
            raise
        return [_pb_to_file_info(f) for f in resp.files]

    def iterdir(self, prefix: str = "") -> Iterator[FileInfo]:
        """Iterate over files in the volume.

        Same as ``listdir()`` but returns an iterator for lazy consumption.

        Parameters
        ----------
        prefix
            Only return files whose paths start with this prefix.

        Yields
        ------
        FileInfo
            One entry per matching file.
        """
        yield from self.listdir(prefix)

    @contextmanager
    def batch_upload(self) -> Iterator[_BatchUpload]:
        """Context manager for batching multiple file uploads.

        All staged files are uploaded when the context manager exits.

        Examples
        --------
        >>> with vol.batch_upload() as batch:
        ...     batch.put("a.txt", b"hello")
        ...     batch.put("b.txt", b"world")
        """
        batch = _BatchUpload(self)
        yield batch
        batch._flush()

    # -- Volume lifecycle ------------------------------------------------------

    @traced("chalkcompute.volume.delete")
    def delete(self) -> None:
        """Delete this volume and all its contents."""
        try:
            self._client._stub.DeleteVolume(
                pb.DeleteVolumeRequest(name=self._name),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._name)
            raise

    def __repr__(self) -> str:
        return f"Volume(name={self._name!r})"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


_VolumeInfoList = list[VolumeInfo]


class VolumeClient:
    """Client for the Volume gRPC Service."""

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
            gRPC server address (e.g. ``"localhost:50051"`` or
            ``"volumes.example.com:443"``).
        credentials
            Optional TLS/SSL channel credentials. If ``None``, uses insecure
            channel.
        metadata
            Optional default metadata to send with every RPC call.
        options
            Optional gRPC channel options.
        """
        self._target = target
        self._metadata = tuple(metadata) if metadata else ()
        if credentials is not None:
            self._channel = grpc.secure_channel(target, credentials, options=options)
        else:
            self._channel = grpc.insecure_channel(target, options=options)
        self._stub = pb_grpc.VolumeServiceStub(self._channel)
        self._obj_stub = pb_grpc.DataPlaneObjectStorageServiceStub(self._channel)  # type: ignore

    @classmethod
    def from_connect(cls, connect: ConnectClient) -> VolumeClient:
        """Create a ``VolumeClient`` from an already-authenticated ``ConnectClient``."""
        connect._ensure_auth()

        host, _use_tls = grpc_target_from_url(connect._api_server or "")

        return cls(
            host,
            credentials=grpc.ssl_channel_credentials(),
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

    def __enter__(self) -> VolumeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the gRPC channel."""
        self._channel.close()

    @traced("chalkcompute.volume.create")
    def create(self, name: str) -> Volume:
        """Create a new named volume.

        If the volume already exists on the server, behavior depends on the
        server implementation (may return the existing volume or error).

        Parameters
        ----------
        name
            Name for the volume.

        Returns
        -------
        Volume
            A volume handle.
        """
        try:
            resp = self._stub.CreateVolume(
                pb.CreateVolumeRequest(name=name),
                metadata=self._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, name)
            raise
        info = _pb_to_volume_info(resp.volume)
        vol = Volume(info.name, client=self)
        vol._info = info
        return vol

    def from_name(self, name: str) -> Volume:
        """Get a ``Volume`` handle by name.

        Does not validate the volume exists until you call a method on it.
        Use this when you know the volume already exists.

        Parameters
        ----------
        name
            Name of the existing volume.

        Returns
        -------
        Volume
            A volume handle.
        """
        return Volume(name, client=self)

    def lookup(self, name: str) -> Volume:
        """Look up a volume by name, verifying it exists.

        Parameters
        ----------
        name
            Name of the volume.

        Returns
        -------
        Volume
            A volume handle with info pre-fetched.

        Raises
        ------
        VolumeNotFoundError
            If the volume does not exist.
        """
        vol = Volume(name, client=self)
        vol.reload()
        return vol

    def list(self) -> _VolumeInfoList:
        """List all volumes.

        Returns
        -------
        list of VolumeInfo
            One entry per volume.
        """
        resp = self._stub.ListVolumes(
            pb.ListVolumesRequest(),
            metadata=self._metadata,
        )
        return [_pb_to_volume_info(v) for v in resp.volumes]


# ---------------------------------------------------------------------------
# Upload routing helpers
# ---------------------------------------------------------------------------


def _upload_bytes(
    client: VolumeClient, volume_name: str, path: str, data: bytes
) -> None:
    """Upload a file using the appropriate transport.

    Files at or below ``_INLINE_THRESHOLD_BYTES`` are sent via gRPC inline
    ``PutFile(data=...)``.  Larger files use a presigned object-storage upload
    followed by ``PutFile(storageObjectId=...)``.

    Parameters
    ----------
    client
        Volume client used for the upload.
    volume_name
        Name of the destination volume.
    path
        Destination path within the volume.
    data
        File contents to upload.
    """
    if len(data) <= _INLINE_THRESHOLD_BYTES:
        _put_file_inline(client, volume_name, path, data)
    else:
        _put_file_presigned(client, volume_name, path, data)


def _put_file_inline(
    client: VolumeClient, volume_name: str, path: str, data: bytes
) -> None:
    """Upload a file via gRPC inline ``PutFile(data=...)``.

    Parameters
    ----------
    client
        Volume client used for the upload.
    volume_name
        Name of the destination volume.
    path
        Destination path within the volume.
    data
        File contents to upload.
    """
    try:
        client._stub.PutFile(
            pb.PutFileRequest(volume_name=volume_name, path=path, data=data),
            metadata=client._metadata,
        )
    except grpc.RpcError as e:
        _handle_rpc_error(e, volume_name)
        raise
    logger.info("Uploaded %s (inline, %d bytes)", path, len(data))


def _put_file_presigned(
    client: VolumeClient, volume_name: str, path: str, data: bytes
) -> None:
    """Upload a file via a presigned object-storage URL.

    Uses the gRPC ``DataPlaneObjectStorageServiceStub`` for
    ``GetObjectUploadUri``, ``httpx`` for the direct object-storage transfer,
    and gRPC ``VolumeServiceStub`` for the final ``PutFile(storageObjectId=...)``.

    Parameters
    ----------
    client
        Volume client used for the upload.
    volume_name
        Name of the destination volume.
    path
        Destination path within the volume.
    data
        File contents to upload.

    Raises
    ------
    VolumeError
        If the server returns an unsupported object-storage upload type.
    """
    checksum = hashlib.md5(data).hexdigest()

    def _get_uri():  # type: ignore[no-untyped-def]
        return client._obj_stub.GetObjectUploadUri(
            pb.GetObjectUploadUriRequest(
                content_size=len(data),
                hash=checksum,
                part_size=_PRESIGNED_PART_SIZE_BYTES,
            ),
            metadata=client._metadata,
        )

    resp = _get_uri()
    upload_type = resp.WhichOneof("upload")

    if upload_type == "multipart":
        storage_object_id = _upload_multipart(data, resp, _get_uri)
    elif upload_type == "resumable":
        storage_object_id = _upload_resumable(data, resp, _get_uri)
    else:
        raise VolumeError(f"Unsupported object-storage upload type: {upload_type!r}")

    try:
        client._stub.PutFile(
            pb.PutFileRequest(
                volume_name=volume_name,
                path=path,
                storage_object_id=storage_object_id,
            ),
            metadata=client._metadata,
        )
    except grpc.RpcError as e:
        _handle_rpc_error(e, volume_name)
        raise
    logger.info(
        "Uploaded %s (presigned %s, %d bytes, object=%s)",
        path,
        upload_type,
        len(data),
        storage_object_id,
    )


def _upload_multipart(
    data: bytes,
    resp: pb.GetObjectUploadUriResponse,
    refresh: Callable[[], pb.GetObjectUploadUriResponse],
) -> str:
    """Upload via S3-style multipart presigned URLs.

    Returns the storage object ID for the completed upload.
    """
    import httpx

    storage_object_id = resp.storage_object_id
    all_uris = list(resp.multipart.signed_upload_uris)
    part_uris = all_uris[:-1]
    completion_uri = all_uris[-1]
    n_parts = len(part_uris)

    refresh_count = 0
    etags: list[str] = []
    i = 0
    while i < n_parts:
        chunk = data[
            i * _PRESIGNED_PART_SIZE_BYTES : (i + 1) * _PRESIGNED_PART_SIZE_BYTES
        ]
        part_resp = httpx.put(part_uris[i], content=chunk, timeout=300)
        if part_resp.status_code in (200, 204):
            etags.append(part_resp.headers.get("ETag", ""))
            i += 1
            continue
        if (
            part_resp.status_code in (401, 403)
            and refresh_count < _PRESIGNED_MAX_RETRIES
        ):
            refresh_count += 1
            logger.warning(
                "Multipart upload: refreshing signed URLs (attempt %d/%d)",
                refresh_count,
                _PRESIGNED_MAX_RETRIES,
            )
            new_resp = refresh()
            storage_object_id = new_resp.storage_object_id
            all_uris = list(new_resp.multipart.signed_upload_uris)
            part_uris = all_uris[:-1]
            completion_uri = all_uris[-1]
            etags.clear()
            i = 0
            continue
        part_resp.raise_for_status()

    parts_xml = "".join(
        f"<Part><PartNumber>{j + 1}</PartNumber><ETag>{etag}</ETag></Part>"
        for j, etag in enumerate(etags)
    )
    body = f"<CompleteMultipartUpload>{parts_xml}</CompleteMultipartUpload>".encode()
    complete_resp = httpx.post(
        completion_uri,
        content=body,
        headers={"Content-Type": "application/xml"},
        timeout=60,
    )
    complete_resp.raise_for_status()
    logger.info(
        "Multipart upload complete: object=%s parts=%d refreshes=%d",
        storage_object_id,
        n_parts,
        refresh_count,
    )
    return storage_object_id


def _upload_resumable(
    data: bytes,
    resp: pb.GetObjectUploadUriResponse,
    refresh: Callable[[], pb.GetObjectUploadUriResponse],
) -> str:
    """Upload via a GCS resumable signed URL.

    Returns the storage object ID for the completed upload.
    """
    import httpx

    storage_object_id = resp.storage_object_id
    signed_uri = resp.resumable.signed_upload_uri
    refresh_count = 0

    while True:
        init = httpx.post(
            signed_uri,
            content=b"",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": "0",
                "x-goog-resumable": "start",
            },
            timeout=30,
        )
        if not init.is_success:
            if (
                init.status_code in (401, 403)
                and refresh_count < _PRESIGNED_MAX_RETRIES
            ):
                refresh_count += 1
                logger.warning(
                    "Resumable upload: refreshing signed URL (attempt %d/%d)",
                    refresh_count,
                    _PRESIGNED_MAX_RETRIES,
                )
                new_resp = refresh()
                storage_object_id = new_resp.storage_object_id
                signed_uri = new_resp.resumable.signed_upload_uri
                continue
            init.raise_for_status()

        session_uri = init.headers.get("Location")
        if not session_uri:
            raise VolumeError(
                "GCS resumable upload: missing Location header in initiation response"
            )

        put_resp = httpx.put(
            session_uri,
            content=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=600,
        )
        if put_resp.status_code in (200, 201):
            logger.info(
                "Resumable upload complete: object=%s refreshes=%d",
                storage_object_id,
                refresh_count,
            )
            return storage_object_id
        if (
            put_resp.status_code in (401, 403)
            and refresh_count < _PRESIGNED_MAX_RETRIES
        ):
            refresh_count += 1
            logger.warning(
                "Resumable upload: refreshing signed URL (attempt %d/%d)",
                refresh_count,
                _PRESIGNED_MAX_RETRIES,
            )
            new_resp = refresh()
            storage_object_id = new_resp.storage_object_id
            signed_uri = new_resp.resumable.signed_upload_uri
            continue
        put_resp.raise_for_status()


# ---------------------------------------------------------------------------
# Lazy local file upload
# ---------------------------------------------------------------------------


@traced("chalkcompute.upload_lazy_files")
def upload_lazy_files(
    client: VolumeClient,
    image: Image,
    *,
    volume_prefix: str = "files",
) -> list[tuple[str, str]]:
    """Upload an image's lazy local files to volumes.

    Creates one volume per destination directory, uploads all files using the
    shared ``_upload_bytes`` router (gRPC inline for small files, presigned
    object-storage for large ones), and returns the ``(volume_name, mount_path)``
    pairs needed to mount them in a container spec.

    Parameters
    ----------
    client
        Authenticated VolumeClient (gRPC).
    image
        The image whose lazy local files should be uploaded.
    volume_prefix
        Prefix for the generated volume names.

    Returns
    -------
    list[tuple[str, str]]
        Pairs of ``(volume_name, mount_path)`` for each created volume.
    """
    try:
        dir_to_files = image.lazy_local_files_grouped_for_volume_mounts()
    except ValueError as e:
        raise VolumeError(str(e)) from e

    if not dir_to_files:
        return []

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _put_file(vol_name: str, lf: LazyLocalFile, dest_dir: str) -> None:
        content = Path(lf.src).read_bytes()
        rel_path = os.path.relpath(lf.dest, dest_dir)
        _upload_bytes(client, vol_name, rel_path, content)

    volumes: list[tuple[str, str]] = []
    for dest_dir, files in dir_to_files.items():
        vol = client.create(f"{volume_prefix}-{uuid.uuid4().hex[:12]}")

        with ThreadPoolExecutor(max_workers=32) as pool:
            futures = [pool.submit(_put_file, vol.name, lf, dest_dir) for lf in files]
            for fut in as_completed(futures):
                fut.result()

        volumes.append((vol.name, dest_dir))

    return volumes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pb_to_volume_info(info: pb.VolumeInfo) -> VolumeInfo:
    created_at = None
    if info.HasField("created_at"):
        created_at = info.created_at.ToDatetime(tzinfo=datetime.timezone.utc)
    return VolumeInfo(
        name=info.name,
        created_at=created_at,
    )


def _pb_to_file_info(info: pb.FileInfo) -> FileInfo:
    updated_at = None
    if info.HasField("updated_at"):
        updated_at = info.updated_at.ToDatetime(tzinfo=datetime.timezone.utc)
    return FileInfo(
        path=info.path,
        size=info.size,
        updated_at=updated_at,
    )


def _handle_rpc_error(e: grpc.RpcError, volume_name: str) -> None:
    raise_for_grpc_error(
        e,
        {
            grpc.StatusCode.NOT_FOUND: (
                VolumeNotFoundError,
                f"Volume {volume_name!r} not found",
            ),
        },
    )


def connect(
    target: str = "localhost:50051",
    *,
    credentials: grpc.ChannelCredentials | None = None,
    metadata: _GrpcMetadata | None = None,
) -> VolumeClient:
    """Create a new ``VolumeClient``.

    Parameters
    ----------
    target
        gRPC server address.
    credentials
        Optional TLS/SSL channel credentials.
    metadata
        Optional default metadata to send with every RPC call.

    Returns
    -------
    VolumeClient
        A new client instance.
    """
    return VolumeClient(target, credentials=credentials, metadata=metadata)
