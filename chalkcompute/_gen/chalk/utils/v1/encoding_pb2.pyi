from google.protobuf import descriptor_pb2 as _descriptor_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Mapping as _Mapping, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor
ENCODING_FIELD_NUMBER: _ClassVar[int]
encoding: _descriptor.FieldDescriptor

class StringEncoding(_message.Message):
    __slots__ = ("mapping",)
    class MappingEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: int
        value: str
        def __init__(self, key: _Optional[int] = ..., value: _Optional[str] = ...) -> None: ...

    MAPPING_FIELD_NUMBER: _ClassVar[int]
    mapping: _containers.ScalarMap[int, str]
    def __init__(self, mapping: _Optional[_Mapping[int, str]] = ...) -> None: ...
