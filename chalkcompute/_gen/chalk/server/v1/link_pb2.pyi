from chalkcompute._gen.chalk.utils.v1 import sensitive_pb2 as _sensitive_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class LinkSessionStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    LINK_SESSION_STATUS_UNSPECIFIED: _ClassVar[LinkSessionStatus]
    LINK_SESSION_STATUS_PENDING: _ClassVar[LinkSessionStatus]
    LINK_SESSION_STATUS_SUCCESS: _ClassVar[LinkSessionStatus]
    LINK_SESSION_STATUS_FAILED: _ClassVar[LinkSessionStatus]
    LINK_SESSION_STATUS_NOT_FOUND: _ClassVar[LinkSessionStatus]
    LINK_SESSION_STATUS_FORBIDDEN: _ClassVar[LinkSessionStatus]

LINK_SESSION_STATUS_UNSPECIFIED: LinkSessionStatus
LINK_SESSION_STATUS_PENDING: LinkSessionStatus
LINK_SESSION_STATUS_SUCCESS: LinkSessionStatus
LINK_SESSION_STATUS_FAILED: LinkSessionStatus
LINK_SESSION_STATUS_NOT_FOUND: LinkSessionStatus
LINK_SESSION_STATUS_FORBIDDEN: LinkSessionStatus

class LinkToken(_message.Message):
    __slots__ = ("name", "client_id", "client_secret", "api_server", "active_environment", "valid_until")
    NAME_FIELD_NUMBER: _ClassVar[int]
    CLIENT_ID_FIELD_NUMBER: _ClassVar[int]
    CLIENT_SECRET_FIELD_NUMBER: _ClassVar[int]
    API_SERVER_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_ENVIRONMENT_FIELD_NUMBER: _ClassVar[int]
    VALID_UNTIL_FIELD_NUMBER: _ClassVar[int]
    name: str
    client_id: str
    client_secret: str
    api_server: str
    active_environment: str
    valid_until: _timestamp_pb2.Timestamp
    def __init__(
        self,
        name: _Optional[str] = ...,
        client_id: _Optional[str] = ...,
        client_secret: _Optional[str] = ...,
        api_server: _Optional[str] = ...,
        active_environment: _Optional[str] = ...,
        valid_until: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
    ) -> None: ...

class GetLinkSessionRequest(_message.Message):
    __slots__ = ("link_code", "project_name")
    LINK_CODE_FIELD_NUMBER: _ClassVar[int]
    PROJECT_NAME_FIELD_NUMBER: _ClassVar[int]
    link_code: str
    project_name: str
    def __init__(self, link_code: _Optional[str] = ..., project_name: _Optional[str] = ...) -> None: ...

class GetLinkSessionResponse(_message.Message):
    __slots__ = ("status", "message", "token", "session_id")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    status: LinkSessionStatus
    message: str
    token: LinkToken
    session_id: str
    def __init__(
        self,
        status: _Optional[_Union[LinkSessionStatus, str]] = ...,
        message: _Optional[str] = ...,
        token: _Optional[_Union[LinkToken, _Mapping]] = ...,
        session_id: _Optional[str] = ...,
    ) -> None: ...

class CreateLinkSessionRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class CreateLinkSessionResponse(_message.Message):
    __slots__ = ("link_code", "auth_link", "expires_at", "session_id")
    LINK_CODE_FIELD_NUMBER: _ClassVar[int]
    AUTH_LINK_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    link_code: str
    auth_link: str
    expires_at: _timestamp_pb2.Timestamp
    session_id: str
    def __init__(
        self,
        link_code: _Optional[str] = ...,
        auth_link: _Optional[str] = ...,
        expires_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        session_id: _Optional[str] = ...,
    ) -> None: ...
