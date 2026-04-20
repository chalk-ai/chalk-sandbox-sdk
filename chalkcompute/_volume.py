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

import builtins
import datetime
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import grpc

from chalkcompute._connect import ConnectClient, grpc_target_from_url
from chalkcompute._gen.chalk.volume.v1 import volume_pb2 as pb
from chalkcompute._gen.chalk.volume.v1 import volume_pb2_grpc as pb_grpc
from chalkcompute._tracing import traced

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VolumeError(Exception):
    """Base exception for volume errors."""


class VolumeNotFoundError(VolumeError):
    """Raised when a volume is not found."""


class FileNotFoundError_(VolumeError):
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
        self._client = client or VolumeClient.from_env()
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
        try:
            self._client._stub.PutFile(
                pb.PutFileRequest(volume_name=self._name, path=path, data=data),
                metadata=self._client._metadata,
            )
        except grpc.RpcError as e:
            _handle_rpc_error(e, self._name)
            raise
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


class VolumeClient:
    """Client for the Volume gRPC Service."""

    def __init__(
        self,
        target: str = "localhost:50051",
        *,
        credentials: grpc.ChannelCredentials | None = None,
        metadata: builtins.list[tuple[str, str]] | None = None,
        options: builtins.list[tuple[str, builtins.int]] | None = None,
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
        self._metadata = metadata
        if credentials is not None:
            self._channel = grpc.secure_channel(target, credentials, options=options)
        else:
            self._channel = grpc.insecure_channel(target, options=options)
        self._stub = pb_grpc.VolumeServiceStub(self._channel)

    @classmethod
    def from_env(cls) -> VolumeClient:
        """Create a ``VolumeClient`` from standard ``CHALK_*`` environment vars."""
        if os.environ.get("CHALK_API_SERVER") is None:
            raise ValueError(
                "CHALK_API_SERVER must be set to construct Volume from env"
            )

        connect = ConnectClient()
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

    def list(self) -> builtins.list[VolumeInfo]:
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
    assert isinstance(e, grpc.Call)
    if e.code() == grpc.StatusCode.NOT_FOUND:
        raise VolumeNotFoundError(f"Volume {volume_name!r} not found") from e


def connect(
    target: str = "localhost:50051",
    *,
    credentials: grpc.ChannelCredentials | None = None,
    metadata: builtins.list[tuple[str, str]] | None = None,
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
