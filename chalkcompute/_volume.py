"""Client utilities for Chalk volumes backed by object storage.

Examples
--------
>>> from chalkcompute import VolumeClient
>>> with VolumeClient("localhost:50051") as client:
...     vol = client.create("my-data")
...     vol.put_file("models/latest.bin", model_bytes)
...     vol.put_file("config.json", b'{"key": "value"}')
...     with vol.batch_upload() as batch:
...         batch.put("data/a.csv", csv_a)
...         batch.put("data/b.csv", csv_b)
...         batch.put("data/c.csv", csv_c)
...     data = vol.read_file("models/latest.bin")
...     for f in vol.listdir("data/"):
...         print(f"{f.path}  {f.size} bytes")
...     vol.remove_file("config.json")
...     vol.delete()
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import uuid
from collections.abc import Callable, Generator, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import grpc
import httpx

from chalkcompute._connect import (
    ConnectClient,
    grpc_target_from_url,
    raise_for_grpc_error,
)
from chalkcompute._gen.chalk.volume.v1 import volume_pb2 as pb
from chalkcompute._gen.chalk.volume.v1 import volume_pb2_grpc as pb_grpc
from chalkcompute._image import Image, LazyLocalFile
from chalkcompute._tracing import _instrumented

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
_INLINE_THRESHOLD_BYTES: int = 2 * 1024 * 1024  # 8 MB

#: Part size sent to GetObjectUploadUri for multipart uploads.
_PRESIGNED_PART_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB

#: Maximum number of signed-URL refreshes before giving up on a presigned upload.
_PRESIGNED_MAX_RETRIES: int = 3

#: Maximum concurrent threads for multipart part uploads and lazy file uploads.
_UPLOAD_MAX_WORKERS: int = 32

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
    """A volume to mount into a scaling group, with its mount type attached.

    The mount ``type`` is a property of the volume, not of the request being
    built or of a transient driver. Carrying it on the volume itself is the
    single source of truth: every create/update request for a given scaling
    group stamps the same type onto the same volume name, so the backend
    ChalkScalingGroup CR does not flip-flop between ``chalkfs`` and
    ``versioned_chalkfs`` and re-roll endlessly.

    Attributes
    ----------
    name
        Volume name.
    mount_path
        Absolute path inside the container where the volume is mounted.
    type
        Mount type: ``"chalkfs"`` (non-versioned) or ``"versioned_chalkfs"``
        (versioned).
    """

    name: str
    mount_path: str
    type: str

    def to_spec_dict(self) -> dict[str, str]:
        """Render this mount as a container-spec ``volumes`` entry."""
        return {"name": self.name, "mountPath": self.mount_path, "type": self.type}


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


# ---------------------------------------------------------------------------
# Batch upload context manager
# ---------------------------------------------------------------------------


class _BatchUpload:
    """Accumulates put_file operations and executes them on exit."""

    def __init__(self, volume: SimpleVolume) -> None:
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


class SimpleVolume:
    """A handle to a named volume, providing file operations.

    The v1 implementation: a flat, path-keyed object store with no version
    history. For snapshot/version semantics use :class:`VersionedVolume`.

    Volumes are persistent storage backed by object storage. Obtain an
    instance via :meth:`VolumeClient.create` or :meth:`VolumeClient.get`.

    Examples
    --------
    Create a volume, upload files, and read them back:

    >>> from chalkcompute import VolumeClient
    >>> client = VolumeClient(target="volumes.example.com:443")
    >>> vol = client.create("models")
    >>> vol.put_file("config.yaml", b"learning_rate: 0.01\\n")
    >>> vol.read_file("config.yaml")
    b'learning_rate: 0.01\\n'
    >>> [f.path for f in vol.listdir()]
    ['config.yaml']

    Batch-upload multiple files atomically:

    >>> with vol.batch_upload() as batch:
    ...     batch.put("a.txt", b"hello")
    ...     batch.put("b.txt", b"world")
    """

    def __init__(
        self,
        name: str,
        client: VolumeClient | None = None,
        *,
        create_if_missing: bool = True,
        chalk_client: Any | None = None,
    ) -> None:
        if not name or not name.strip():
            raise ValueError("volume name cannot be empty")
        self._client = client or VolumeClient.from_connect(ConnectClient(chalk_client=chalk_client))
        self._name = name
        self._info: VolumeInfo | None = None
        self._create_if_missing = create_if_missing
        self._closed = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def info(self) -> VolumeInfo:
        """Return cached volume metadata, fetching it on first access.

        Returns
        -------
        VolumeInfo
            Metadata for this volume.
        """
        self._raise_if_closed()
        if self._info is None:
            self._info = self._fetch_info()
        return self._info

    def reload(self) -> VolumeInfo:
        """Refresh volume metadata from the server.

        Returns
        -------
        VolumeInfo
            Fresh metadata for this volume.
        """
        self._info = self._fetch_info()
        return self._info

    def _fetch_info(self) -> VolumeInfo:
        self._raise_if_closed()
        try:
            resp = self._client._stub.GetVolume(
                pb.GetVolumeRequest(name=self._name),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            code = getattr(e, "code", lambda: None)()
            if not self._create_if_missing or code != grpc.StatusCode.NOT_FOUND:
                _handle_rpc_error(e, self._name)
                raise
        else:
            return _pb_to_volume_info(resp.volume)

        try:
            resp = self._client._stub.CreateVolume(
                pb.CreateVolumeRequest(name=self._name),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._name)
            raise
        return _pb_to_volume_info(resp.volume)

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise VolumeClosedError(f"Volume {self._name!r} is closed")

    def _ensure_ready(self) -> None:
        self._raise_if_closed()
        if self._info is None:
            self._info = self._fetch_info()

    # -- File operations -------------------------------------------------------

    @_instrumented("chalkcompute.volume.read_file")
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
        self._ensure_ready()
        return _read_file_bytes(self._client, self._name, path)

    @_instrumented("chalkcompute.volume.put_file")
    def put_file(self, path: str, data: bytes | str) -> SimpleVolume:
        """Write a file into the volume.

        Small files (<= ``_INLINE_THRESHOLD_BYTES``) are uploaded via gRPC
        inline ``PutFile(data=...)``. Large files use a presigned
        object-storage URL (``GetObjectUploadUri`` -> direct S3/GCS upload ->
        ``PutFile(storageObjectId=...)``).

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
        self._ensure_ready()
        _upload_bytes(self._client, self._name, path, data)
        return self

    def put_file_from_path(self, path: str, local_path: str | Path) -> SimpleVolume:
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

    def remove_file(self, path: str) -> SimpleVolume:
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
        self._ensure_ready()
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
        self._ensure_ready()
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
    def batch_upload(self) -> Generator[_BatchUpload, None, None]:
        """Context manager for batching multiple file uploads.

        All staged files are uploaded when the context manager exits.

        Examples
        --------
        >>> with vol.batch_upload() as batch:
        ...     batch.put("a.txt", b"hello")
        ...     batch.put("b.txt", b"world")
        """
        self._raise_if_closed()
        batch = _BatchUpload(self)
        yield batch
        batch._flush()

    # -- Volume lifecycle ------------------------------------------------------

    @_instrumented("chalkcompute.volume.delete")
    def delete(self) -> None:
        """Delete this volume and all of its contents."""
        self._ensure_ready()
        try:
            self._client._stub.DeleteVolume(
                pb.DeleteVolumeRequest(name=self._name),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._name)
            raise

    def close(self) -> None:
        """Close the underlying client used by this Volume."""
        if not self._closed:
            self._client.close()
        self._closed = True

    # -- Versioned-volume API stubs --------------------------------------------
    # The v1 backend has no version history. These exist so the generic
    # ``Volume`` factory can return either backend and code can program against
    # a single interface; callers using the v1 backend explicitly opt out of
    # version-aware operations.

    def versions(self, *, limit: int = 100) -> list:
        raise NotImplementedError("SimpleVolume has no version history")

    def latest_version(self):
        raise NotImplementedError("SimpleVolume has no version history")

    def at_version(self, version_id: int) -> SimpleVolume:
        raise NotImplementedError("SimpleVolume has no version history")

    def __enter__(self) -> SimpleVolume:
        self._raise_if_closed()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"SimpleVolume(name={self._name!r})"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


_VolumeInfoList = list[VolumeInfo]
_VolumeMounts = list[VolumeMount]


class VolumeDriver(Protocol):
    """Internal protocol for clients that back container/sandbox mounts.

    Satisfied by both :class:`VolumeClient` (mount type ``chalkfs``) and
    :class:`VersionedVolumeClient` (mount type ``versioned_chalkfs``).
    Not re-exported from ``chalkcompute``.
    """

    mount_type: str

    def upload_lazy_files(self, image: Image, *, volume_prefix: str) -> "list[VolumeMount]": ...

    def delete_volume(self, name: str) -> None: ...

    def exists(self, name: str) -> bool: ...

    def close(self) -> None: ...


def resolve_referenced_volume_type(connect: ConnectClient, name: str) -> str:
    """Return the actual mount type of an existing volume by probing the
    versioned (v2) service first, then non-versioned (v1).

    A user-referenced volume (supplied as a ``(name, mount_path)`` tuple)
    has no type of its own at construction time. Its mount type is a property
    of the backend that actually stores it, so we look it up at deploy time
    instead of stamping a hardcoded default. Defaulting here is exactly the
    bug this avoids: a non-versioned ``chalkfs`` volume sent as
    ``versioned_chalkfs`` cannot be mounted by the versioned FUSE sidecar.

    Parameters
    ----------
    connect
        Authenticated client reused for both probes.
    name
        Name of the existing volume to classify.

    Returns
    -------
    str
        ``"versioned_chalkfs"`` if the volume lives in the v2 service, or
        ``"chalkfs"`` if it lives only in the v1 service.

    Raises
    ------
    VolumeError
        If the volume is not found in either service. We never fall back to a
        default mount type for a referenced volume.
    grpc.RpcError
        Propagated for any non-``NOT_FOUND`` probe failure (auth, transport,
        etc.), so a transient error is never silently treated as "absent".
    """
    from chalkcompute._vvolume import VersionedVolumeClient

    v2 = VersionedVolumeClient.from_connect(connect)
    if v2.exists(name):
        return v2.mount_type

    v1 = VolumeClient.from_connect(connect)
    if v1.exists(name):
        return v1.mount_type

    raise VolumeError(
        f"Referenced volume {name!r} was not found in the versioned "
        f"(v2) or non-versioned (v1) volume service; cannot determine its "
        f"mount type. Check the volume name, or create it before referencing it."
    )


def cleanup_owned_volumes(
    driver: VolumeDriver,
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
        driver.delete_volume(v.name)


def make_volume(
    name: str,
    *,
    versioned: bool = True,
    client: object | None = None,
    version_id: int | None = None,
    ref: str = "main",
    create_if_missing: bool = True,
):
    """Construct a volume handle (re-exported as :data:`chalkcompute.Volume`).

    Defaults to a versioned (v2) volume; pass ``versioned=False`` for the
    path-keyed :class:`SimpleVolume` backend, which raises
    ``NotImplementedError`` for version-aware operations.

    Parameters
    ----------
    name
        Volume name.
    versioned
        If ``True`` (the default), return a :class:`VersionedVolume`.
        Otherwise return a :class:`SimpleVolume`.
    client
        Optional pre-constructed client matching the chosen backend.
    version_id
        Pin a versioned handle to an immutable version. Versioned only.
    ref
        Mutable ref to read/write through. Versioned only.
    create_if_missing
        If ``True``, create the volume on first access if absent.

    Returns
    -------
    VersionedVolume or SimpleVolume
        A handle for the requested backend.

    Raises
    ------
    ValueError
        If ``version_id`` or a non-default ``ref`` is supplied with
        ``versioned=False``.
    """
    if versioned:
        from chalkcompute._vvolume import VersionedVolume, VersionedVolumeClient

        return VersionedVolume(
            name,
            client=client if isinstance(client, VersionedVolumeClient) else None,
            version_id=version_id,
            ref=ref,
            create_if_missing=create_if_missing,
        )
    if version_id is not None or ref != "main":
        raise ValueError("version_id and ref are only supported for versioned volumes")
    return SimpleVolume(
        name,
        client=client if isinstance(client, VolumeClient) else None,
        create_if_missing=create_if_missing,
    )


class VolumeClient:
    """Client for the Volume gRPC service."""

    mount_type = "chalkfs"

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
        self._chalk_client: Any | None = None
        if credentials is not None:
            self._channel = grpc.secure_channel(target, credentials, options=options)
        else:
            self._channel = grpc.insecure_channel(target, options=options)
        self._stub = pb_grpc.VolumeServiceStub(self._channel)
        self._obj_stub = pb_grpc.DataPlaneObjectStorageServiceStub(self._channel)

    @classmethod
    def from_connect(cls, connect: ConnectClient) -> VolumeClient:
        """Create a ``VolumeClient`` from an already-authenticated ``ConnectClient``.

        Parameters
        ----------
        connect
            An authenticated ``ConnectClient`` instance. Auth is
            ensured automatically if not already done.

        Returns
        -------
        VolumeClient
            A new client with TLS credentials and metadata derived
            from the ``ConnectClient``'s token and environment.
        """
        connect._ensure_auth()

        host, use_tls = grpc_target_from_url(connect._api_server or "")

        client = cls(
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
        client._chalk_client = getattr(connect, "_chalk_client", None)
        return client

    def _refresh(self) -> None:
        """Re-authenticate and rebuild the gRPC channel.

        Creates a fresh ``ConnectClient``, fetches a new bearer token,
        and replaces the channel, stubs, and metadata in-place.
        """
        connect = ConnectClient(chalk_client=getattr(self, "_chalk_client", None))
        connect._ensure_auth()
        self._channel.close()

        self._metadata = (
            ("authorization", f"Bearer {connect._access_token}"),
            ("x-chalk-env-id", connect._env_id or ""),
            ("x-chalk-server", "go-api"),
        )
        self._target, use_tls = grpc_target_from_url(connect._api_server or "")
        options = [
            ("grpc.max_send_message_length", -1),
            ("grpc.max_receive_message_length", -1),
        ]
        if use_tls:
            self._channel = grpc.secure_channel(
                self._target,
                grpc.ssl_channel_credentials(),
                options=options,
            )
        else:
            self._channel = grpc.insecure_channel(
                self._target,
                options=options,
            )
        self._stub = pb_grpc.VolumeServiceStub(self._channel)
        self._obj_stub = pb_grpc.DataPlaneObjectStorageServiceStub(self._channel)

    def __enter__(self) -> VolumeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying gRPC channel"""
        self._channel.close()

    @_instrumented("chalkcompute.volume.create")
    def create(self, name: str) -> SimpleVolume:
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
        vol = SimpleVolume(info.name, client=self, create_if_missing=False)
        vol._info = info
        return vol

    def from_name(self, name: str) -> SimpleVolume:
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
        return SimpleVolume(name, client=self, create_if_missing=False)

    def lookup(self, name: str) -> SimpleVolume:
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
        vol = SimpleVolume(name, client=self, create_if_missing=False)
        vol.reload()
        return vol

    def exists(self, name: str) -> bool:
        """Return whether a non-versioned (v1) volume with ``name`` exists.

        Probes ``GetVolume`` and maps ``NOT_FOUND`` to ``False``. Any other
        RPC error propagates: callers resolving a referenced volume's type
        must not silently treat an auth/transport failure as "absent".
        """
        try:
            self._stub.GetVolume(
                pb.GetVolumeRequest(name=name),
                metadata=self._metadata,
            )
        except grpc.RpcError as e:
            code = getattr(e, "code", lambda: None)()
            if code == grpc.StatusCode.NOT_FOUND:
                return False
            raise
        return True

    @_instrumented("chalkcompute.volume.upload_lazy_files")
    def upload_lazy_files(self, image: Image, *, volume_prefix: str = "files") -> _VolumeMounts:
        """Upload an image's lazy local files into v1 volumes.

        Creates one volume per destination directory and uploads each
        file through the inline-vs-presigned router.

        Parameters
        ----------
        image
            Image whose lazy local files should be uploaded.
        volume_prefix
            Prefix used when naming the generated volumes.

        Returns
        -------
        list of VolumeMount
            One :class:`VolumeMount` per created volume. Each carries its own
            mount type (``"chalkfs"`` for this v1 backend), so callers mount it
            with the type that travels on the volume rather than re-deriving a
            type at the request-building site.
        """
        try:
            dir_to_files = image.lazy_local_files_grouped_for_volume_mounts()
        except ValueError as e:
            raise VolumeError(str(e)) from e

        def _put(vol_name: str, lf: LazyLocalFile, dest_dir: str) -> None:
            content = Path(lf.src).read_bytes()
            _upload_bytes(
                self,
                vol_name,
                os.path.relpath(lf.dest, dest_dir),
                content,
            )

        volumes: _VolumeMounts = []
        for dest_dir, files in dir_to_files.items():
            vol = self.create(f"{volume_prefix}-{uuid.uuid4().hex[:12]}")
            with ThreadPoolExecutor(max_workers=_UPLOAD_MAX_WORKERS) as pool:
                for fut in as_completed(pool.submit(_put, vol.name, lf, dest_dir) for lf in files):
                    fut.result()
            volumes.append(VolumeMount(name=vol.name, mount_path=dest_dir, type=self.mount_type))
        return volumes

    def delete_volume(self, name: str) -> None:
        """Delete a volume by name."""
        try:
            self._stub.DeleteVolume(
                pb.DeleteVolumeRequest(name=name),
                metadata=self._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, name)
            raise

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


def _read_file_bytes(client: VolumeClient, volume_name: str, path: str) -> bytes:
    """Read a file using the transport selected by the server.

    Parameters
    ----------
    client
        Volume client used for the download.
    volume_name
        Name of the source volume.
    path
        Path to read within the volume.

    Returns
    -------
    bytes
        Raw file contents.
    """
    try:
        resp = client._stub.GetFile(
            pb.GetFileRequest(volume_name=volume_name, path=path, get_signed_uri=True),
            metadata=client._metadata,
        )
    except grpc.RpcError as e:
        _handle_rpc_error(e, volume_name)
        raise

    content_type = resp.WhichOneof("content")
    if content_type == "signed_download_uri":
        return _read_file_from_signed_uri(resp.signed_download_uri)
    if content_type == "data":
        return resp.data
    raise VolumeError(f"Unsupported file download response type: {content_type!r}")


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


def _upload_bytes(client: VolumeClient, volume_name: str, path: str, data: bytes) -> None:
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


def _put_file_inline(client: VolumeClient, volume_name: str, path: str, data: bytes) -> None:
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


def _put_file_presigned(client: VolumeClient, volume_name: str, path: str, data: bytes) -> None:
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

    client._refresh()
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

    Parts are uploaded concurrently. On 401/403 from S3, all signed URLs
    are refreshed and parts are re-uploaded from scratch.

    Parameters
    ----------
    data
        File contents to upload.
    resp
        Initial multipart upload configuration returned by the server.
    refresh
        Callback that fetches a fresh upload configuration after signed URLs
        expire.

    Returns
    -------
    str
        Storage object ID for the completed upload.
    """
    from concurrent.futures import ThreadPoolExecutor

    storage_object_id = resp.storage_object_id
    all_uris = list(resp.multipart.signed_upload_uris)
    part_uris = all_uris[:-1]
    completion_uri = all_uris[-1]
    n_parts = len(part_uris)

    def _put_part(i: int) -> tuple[int, str]:
        """Upload part *i* and return ``(index, etag)``."""
        chunk = data[i * _PRESIGNED_PART_SIZE_BYTES : (i + 1) * _PRESIGNED_PART_SIZE_BYTES]
        r = httpx.put(part_uris[i], content=chunk, timeout=300)
        if r.status_code in (401, 403):
            r.raise_for_status()
        r.raise_for_status()
        return i, r.headers.get("ETag", "")

    refresh_count = 0
    while True:
        try:
            etags_by_index: dict[int, str] = {}
            with ThreadPoolExecutor(max_workers=_UPLOAD_MAX_WORKERS) as pool:
                for i, etag in pool.map(_put_part, range(n_parts)):
                    etags_by_index[i] = etag
            break
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403) and refresh_count < _PRESIGNED_MAX_RETRIES:
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
                continue
            raise

    etags = [etags_by_index[i] for i in range(n_parts)]
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

    Parameters
    ----------
    data
        File contents to upload.
    resp
        Initial resumable upload configuration returned by the server.
    refresh
        Callback that fetches a fresh upload configuration after the signed URL
        expires.

    Returns
    -------
    str
        Storage object ID for the completed upload.
    """
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
            if init.status_code in (401, 403) and refresh_count < _PRESIGNED_MAX_RETRIES:
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
        if put_resp.status_code in (401, 403) and refresh_count < _PRESIGNED_MAX_RETRIES:
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
