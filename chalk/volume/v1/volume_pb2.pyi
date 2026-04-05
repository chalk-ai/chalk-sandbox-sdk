import datetime

from chalk.auth.v1 import permissions_pb2 as _permissions_pb2
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
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...

class PutFileRequest(_message.Message):
    __slots__ = ("volume_name", "path", "data")
    VOLUME_NAME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    volume_name: str
    path: str
    data: bytes
    def __init__(self, volume_name: _Optional[str] = ..., path: _Optional[str] = ..., data: _Optional[bytes] = ...) -> None: ...

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
