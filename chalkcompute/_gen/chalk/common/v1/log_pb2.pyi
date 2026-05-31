from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import (
    ClassVar as _ClassVar,
    Mapping as _Mapping,
    Optional as _Optional,
    Union as _Union,
)

DESCRIPTOR: _descriptor.FileDescriptor

class LogEntry(_message.Message):
    __slots__ = ("id", "severity", "timestamp", "message", "labels")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(
            self, key: _Optional[str] = ..., value: _Optional[str] = ...
        ) -> None: ...

    ID_FIELD_NUMBER: _ClassVar[int]
    SEVERITY_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    id: str
    severity: str
    timestamp: _timestamp_pb2.Timestamp
    message: str
    labels: _containers.ScalarMap[str, str]
    def __init__(
        self,
        id: _Optional[str] = ...,
        severity: _Optional[str] = ...,
        timestamp: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        message: _Optional[str] = ...,
        labels: _Optional[_Mapping[str, str]] = ...,
    ) -> None: ...
