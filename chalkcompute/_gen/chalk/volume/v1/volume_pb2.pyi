import datetime

from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class VolumeInfo(_message.Message):
    __slots__ = ("name", "created_at")
    NAME_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    name: str
    created_at: _timestamp_pb2.Timestamp
    def __init__(self, name: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class FileInfo(_message.Message):
    __slots__ = ("path", "size", "updated_at")
    PATH_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    path: str
    size: int
    updated_at: _timestamp_pb2.Timestamp
    def __init__(self, path: _Optional[str] = ..., size: _Optional[int] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class CreateVolumeRequest(_message.Message):
    __slots__ = ("name",)
    NAME_FIELD_NUMBER: _ClassVar[int]
    name: str
    def __init__(self, name: _Optional[str] = ...) -> None: ...

class CreateVolumeResponse(_message.Message):
    __slots__ = ("volume",)
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    volume: VolumeInfo
    def __init__(self, volume: _Optional[_Union[VolumeInfo, _Mapping]] = ...) -> None: ...

class GetVolumeRequest(_message.Message):
    __slots__ = ("name",)
    NAME_FIELD_NUMBER: _ClassVar[int]
    name: str
    def __init__(self, name: _Optional[str] = ...) -> None: ...

class GetVolumeResponse(_message.Message):
    __slots__ = ("volume",)
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    volume: VolumeInfo
    def __init__(self, volume: _Optional[_Union[VolumeInfo, _Mapping]] = ...) -> None: ...

class ListVolumesRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListVolumesResponse(_message.Message):
    __slots__ = ("volumes",)
    VOLUMES_FIELD_NUMBER: _ClassVar[int]
    volumes: _containers.RepeatedCompositeFieldContainer[VolumeInfo]
    def __init__(self, volumes: _Optional[_Iterable[_Union[VolumeInfo, _Mapping]]] = ...) -> None: ...

class DeleteVolumeRequest(_message.Message):
    __slots__ = ("name",)
    NAME_FIELD_NUMBER: _ClassVar[int]
    name: str
    def __init__(self, name: _Optional[str] = ...) -> None: ...

class DeleteVolumeResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListFilesRequest(_message.Message):
    __slots__ = ("volume_name", "prefix")
    VOLUME_NAME_FIELD_NUMBER: _ClassVar[int]
    PREFIX_FIELD_NUMBER: _ClassVar[int]
    volume_name: str
    prefix: str
    def __init__(self, volume_name: _Optional[str] = ..., prefix: _Optional[str] = ...) -> None: ...

class ListFilesResponse(_message.Message):
    __slots__ = ("files",)
    FILES_FIELD_NUMBER: _ClassVar[int]
    files: _containers.RepeatedCompositeFieldContainer[FileInfo]
    def __init__(self, files: _Optional[_Iterable[_Union[FileInfo, _Mapping]]] = ...) -> None: ...

class GetFileRequest(_message.Message):
    __slots__ = ("volume_name", "path")
    VOLUME_NAME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    volume_name: str
    path: str
    def __init__(self, volume_name: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class GetFileResponse(_message.Message):
    __slots__ = ("data", "signed_download_uri")
    DATA_FIELD_NUMBER: _ClassVar[int]
    SIGNED_DOWNLOAD_URI_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    signed_download_uri: str
    def __init__(self, data: _Optional[bytes] = ..., signed_download_uri: _Optional[str] = ...) -> None: ...

class PutFileRequest(_message.Message):
    __slots__ = ("volume_name", "path", "data", "storage_object_id")
    VOLUME_NAME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    STORAGE_OBJECT_ID_FIELD_NUMBER: _ClassVar[int]
    volume_name: str
    path: str
    data: bytes
    storage_object_id: str
    def __init__(self, volume_name: _Optional[str] = ..., path: _Optional[str] = ..., data: _Optional[bytes] = ..., storage_object_id: _Optional[str] = ...) -> None: ...

class PutFileResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class RemoveFileRequest(_message.Message):
    __slots__ = ("volume_name", "path")
    VOLUME_NAME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    volume_name: str
    path: str
    def __init__(self, volume_name: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class RemoveFileResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetObjectUploadUriRequest(_message.Message):
    __slots__ = ("content_size", "hash", "part_size")
    CONTENT_SIZE_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    PART_SIZE_FIELD_NUMBER: _ClassVar[int]
    content_size: int
    hash: str
    part_size: int
    def __init__(self, content_size: _Optional[int] = ..., hash: _Optional[str] = ..., part_size: _Optional[int] = ...) -> None: ...

class MultipartUpload(_message.Message):
    __slots__ = ("signed_upload_uris",)
    SIGNED_UPLOAD_URIS_FIELD_NUMBER: _ClassVar[int]
    signed_upload_uris: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, signed_upload_uris: _Optional[_Iterable[str]] = ...) -> None: ...

class ResumableUpload(_message.Message):
    __slots__ = ("signed_upload_uri",)
    SIGNED_UPLOAD_URI_FIELD_NUMBER: _ClassVar[int]
    signed_upload_uri: str
    def __init__(self, signed_upload_uri: _Optional[str] = ...) -> None: ...

class GetObjectUploadUriResponse(_message.Message):
    __slots__ = ("storage_object_id", "multipart", "resumable")
    STORAGE_OBJECT_ID_FIELD_NUMBER: _ClassVar[int]
    MULTIPART_FIELD_NUMBER: _ClassVar[int]
    RESUMABLE_FIELD_NUMBER: _ClassVar[int]
    storage_object_id: str
    multipart: MultipartUpload
    resumable: ResumableUpload
    def __init__(self, storage_object_id: _Optional[str] = ..., multipart: _Optional[_Union[MultipartUpload, _Mapping]] = ..., resumable: _Optional[_Union[ResumableUpload, _Mapping]] = ...) -> None: ...

class GetObjectDownloadUriRequest(_message.Message):
    __slots__ = ("storage_object_id",)
    STORAGE_OBJECT_ID_FIELD_NUMBER: _ClassVar[int]
    storage_object_id: str
    def __init__(self, storage_object_id: _Optional[str] = ...) -> None: ...

class GetObjectDownloadUriResponse(_message.Message):
    __slots__ = ("signed_download_uri",)
    SIGNED_DOWNLOAD_URI_FIELD_NUMBER: _ClassVar[int]
    signed_download_uri: str
    def __init__(self, signed_download_uri: _Optional[str] = ...) -> None: ...
