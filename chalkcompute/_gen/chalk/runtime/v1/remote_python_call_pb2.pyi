from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
from chalkcompute._gen.chalk.common.v1 import chalk_error_pb2 as _chalk_error_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RemoteCallStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    REMOTE_CALL_STATUS_UNSPECIFIED: _ClassVar[RemoteCallStatus]
    REMOTE_CALL_STATUS_PENDING: _ClassVar[RemoteCallStatus]
    REMOTE_CALL_STATUS_RUNNING: _ClassVar[RemoteCallStatus]
    REMOTE_CALL_STATUS_COMPLETED: _ClassVar[RemoteCallStatus]
    REMOTE_CALL_STATUS_FAILED: _ClassVar[RemoteCallStatus]
REMOTE_CALL_STATUS_UNSPECIFIED: RemoteCallStatus
REMOTE_CALL_STATUS_PENDING: RemoteCallStatus
REMOTE_CALL_STATUS_RUNNING: RemoteCallStatus
REMOTE_CALL_STATUS_COMPLETED: RemoteCallStatus
REMOTE_CALL_STATUS_FAILED: RemoteCallStatus

class CallFunctionRequest(_message.Message):
    __slots__ = ("name", "feather_stream")
    NAME_FIELD_NUMBER: _ClassVar[int]
    FEATHER_STREAM_FIELD_NUMBER: _ClassVar[int]
    name: str
    feather_stream: bytes
    def __init__(self, name: _Optional[str] = ..., feather_stream: _Optional[bytes] = ...) -> None: ...

class CallFunctionResponse(_message.Message):
    __slots__ = ("feather_stream",)
    FEATHER_STREAM_FIELD_NUMBER: _ClassVar[int]
    feather_stream: bytes
    def __init__(self, feather_stream: _Optional[bytes] = ...) -> None: ...

class RemoteCallArgs(_message.Message):
    __slots__ = ("feather_bytes", "storage_object_id")
    FEATHER_BYTES_FIELD_NUMBER: _ClassVar[int]
    STORAGE_OBJECT_ID_FIELD_NUMBER: _ClassVar[int]
    feather_bytes: bytes
    storage_object_id: str
    def __init__(self, feather_bytes: _Optional[bytes] = ..., storage_object_id: _Optional[str] = ...) -> None: ...

class EnqueueRemoteCallRequest(_message.Message):
    __slots__ = ("name", "args")
    NAME_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    name: str
    args: RemoteCallArgs
    def __init__(self, name: _Optional[str] = ..., args: _Optional[_Union[RemoteCallArgs, _Mapping]] = ...) -> None: ...

class EnqueueRemoteCallResponse(_message.Message):
    __slots__ = ("call_id",)
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    def __init__(self, call_id: _Optional[str] = ...) -> None: ...

class PollRemoteCallRequest(_message.Message):
    __slots__ = ("call_id", "cursor")
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    cursor: str
    def __init__(self, call_id: _Optional[str] = ..., cursor: _Optional[str] = ...) -> None: ...

class PollRemoteCallResponse(_message.Message):
    __slots__ = ("status", "results", "cursor", "errors")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    RESULTS_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    ERRORS_FIELD_NUMBER: _ClassVar[int]
    status: RemoteCallStatus
    results: _containers.RepeatedCompositeFieldContainer[CallFunctionResponse]
    cursor: str
    errors: _containers.RepeatedCompositeFieldContainer[_chalk_error_pb2.ChalkError]
    def __init__(self, status: _Optional[_Union[RemoteCallStatus, str]] = ..., results: _Optional[_Iterable[_Union[CallFunctionResponse, _Mapping]]] = ..., cursor: _Optional[str] = ..., errors: _Optional[_Iterable[_Union[_chalk_error_pb2.ChalkError, _Mapping]]] = ...) -> None: ...

class PurgeQueueRequest(_message.Message):
    __slots__ = ("function_name", "all")
    FUNCTION_NAME_FIELD_NUMBER: _ClassVar[int]
    ALL_FIELD_NUMBER: _ClassVar[int]
    function_name: str
    all: bool
    def __init__(self, function_name: _Optional[str] = ..., all: bool = ...) -> None: ...

class PurgeQueueResponse(_message.Message):
    __slots__ = ("items_removed_by_function",)
    class ItemsRemovedByFunctionEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    ITEMS_REMOVED_BY_FUNCTION_FIELD_NUMBER: _ClassVar[int]
    items_removed_by_function: _containers.ScalarMap[str, int]
    def __init__(self, items_removed_by_function: _Optional[_Mapping[str, int]] = ...) -> None: ...

class FunctionCallInfo(_message.Message):
    __slots__ = ("call_id", "function_name", "enqueued_at", "status", "result_summary", "trace_id")
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    FUNCTION_NAME_FIELD_NUMBER: _ClassVar[int]
    ENQUEUED_AT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    RESULT_SUMMARY_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    function_name: str
    enqueued_at: _timestamp_pb2.Timestamp
    status: RemoteCallStatus
    result_summary: str
    trace_id: str
    def __init__(self, call_id: _Optional[str] = ..., function_name: _Optional[str] = ..., enqueued_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ..., status: _Optional[_Union[RemoteCallStatus, str]] = ..., result_summary: _Optional[str] = ..., trace_id: _Optional[str] = ...) -> None: ...

class GetRecentCallsRequest(_message.Message):
    __slots__ = ("function_name", "limit")
    FUNCTION_NAME_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    function_name: str
    limit: int
    def __init__(self, function_name: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class GetRecentCallsResponse(_message.Message):
    __slots__ = ("calls",)
    CALLS_FIELD_NUMBER: _ClassVar[int]
    calls: _containers.RepeatedCompositeFieldContainer[FunctionCallInfo]
    def __init__(self, calls: _Optional[_Iterable[_Union[FunctionCallInfo, _Mapping]]] = ...) -> None: ...

class GetCallCountRequest(_message.Message):
    __slots__ = ("function_name",)
    FUNCTION_NAME_FIELD_NUMBER: _ClassVar[int]
    function_name: str
    def __init__(self, function_name: _Optional[str] = ...) -> None: ...

class GetCallCountResponse(_message.Message):
    __slots__ = ("count",)
    COUNT_FIELD_NUMBER: _ClassVar[int]
    count: int
    def __init__(self, count: _Optional[int] = ...) -> None: ...
