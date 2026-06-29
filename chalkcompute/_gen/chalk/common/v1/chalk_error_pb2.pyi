from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import (
    ClassVar as _ClassVar,
    Mapping as _Mapping,
    Optional as _Optional,
    Union as _Union,
)

DESCRIPTOR: _descriptor.FileDescriptor

class ErrorCode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ERROR_CODE_INTERNAL_SERVER_ERROR_UNSPECIFIED: _ClassVar[ErrorCode]
    ERROR_CODE_PARSE_FAILED: _ClassVar[ErrorCode]
    ERROR_CODE_RESOLVER_NOT_FOUND: _ClassVar[ErrorCode]
    ERROR_CODE_INVALID_QUERY: _ClassVar[ErrorCode]
    ERROR_CODE_VALIDATION_FAILED: _ClassVar[ErrorCode]
    ERROR_CODE_RESOLVER_FAILED: _ClassVar[ErrorCode]
    ERROR_CODE_RESOLVER_TIMED_OUT: _ClassVar[ErrorCode]
    ERROR_CODE_UPSTREAM_FAILED: _ClassVar[ErrorCode]
    ERROR_CODE_UNAUTHENTICATED: _ClassVar[ErrorCode]
    ERROR_CODE_UNAUTHORIZED: _ClassVar[ErrorCode]
    ERROR_CODE_CANCELLED: _ClassVar[ErrorCode]
    ERROR_CODE_DEADLINE_EXCEEDED: _ClassVar[ErrorCode]

class ErrorCodeCategory(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ERROR_CODE_CATEGORY_NETWORK_UNSPECIFIED: _ClassVar[ErrorCodeCategory]
    ERROR_CODE_CATEGORY_REQUEST: _ClassVar[ErrorCodeCategory]
    ERROR_CODE_CATEGORY_FIELD: _ClassVar[ErrorCodeCategory]

ERROR_CODE_INTERNAL_SERVER_ERROR_UNSPECIFIED: ErrorCode
ERROR_CODE_PARSE_FAILED: ErrorCode
ERROR_CODE_RESOLVER_NOT_FOUND: ErrorCode
ERROR_CODE_INVALID_QUERY: ErrorCode
ERROR_CODE_VALIDATION_FAILED: ErrorCode
ERROR_CODE_RESOLVER_FAILED: ErrorCode
ERROR_CODE_RESOLVER_TIMED_OUT: ErrorCode
ERROR_CODE_UPSTREAM_FAILED: ErrorCode
ERROR_CODE_UNAUTHENTICATED: ErrorCode
ERROR_CODE_UNAUTHORIZED: ErrorCode
ERROR_CODE_CANCELLED: ErrorCode
ERROR_CODE_DEADLINE_EXCEEDED: ErrorCode
ERROR_CODE_CATEGORY_NETWORK_UNSPECIFIED: ErrorCodeCategory
ERROR_CODE_CATEGORY_REQUEST: ErrorCodeCategory
ERROR_CODE_CATEGORY_FIELD: ErrorCodeCategory

class ChalkException(_message.Message):
    __slots__ = ("kind", "message", "stacktrace", "internal_stacktrace")
    KIND_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    STACKTRACE_FIELD_NUMBER: _ClassVar[int]
    INTERNAL_STACKTRACE_FIELD_NUMBER: _ClassVar[int]
    kind: str
    message: str
    stacktrace: str
    internal_stacktrace: str
    def __init__(
        self,
        kind: _Optional[str] = ...,
        message: _Optional[str] = ...,
        stacktrace: _Optional[str] = ...,
        internal_stacktrace: _Optional[str] = ...,
    ) -> None: ...

class ChalkError(_message.Message):
    __slots__ = (
        "code",
        "category",
        "message",
        "display_primary_key",
        "display_primary_key_fqn",
        "exception",
        "feature",
        "resolver",
    )
    CODE_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_PRIMARY_KEY_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_PRIMARY_KEY_FQN_FIELD_NUMBER: _ClassVar[int]
    EXCEPTION_FIELD_NUMBER: _ClassVar[int]
    FEATURE_FIELD_NUMBER: _ClassVar[int]
    RESOLVER_FIELD_NUMBER: _ClassVar[int]
    code: ErrorCode
    category: ErrorCodeCategory
    message: str
    display_primary_key: str
    display_primary_key_fqn: str
    exception: ChalkException
    feature: str
    resolver: str
    def __init__(
        self,
        code: _Optional[_Union[ErrorCode, str]] = ...,
        category: _Optional[_Union[ErrorCodeCategory, str]] = ...,
        message: _Optional[str] = ...,
        display_primary_key: _Optional[str] = ...,
        display_primary_key_fqn: _Optional[str] = ...,
        exception: _Optional[_Union[ChalkException, _Mapping]] = ...,
        feature: _Optional[str] = ...,
        resolver: _Optional[str] = ...,
    ) -> None: ...
