from google.protobuf import descriptor_pb2 as _descriptor_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AuditLevel(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    AUDIT_LEVEL_UNSPECIFIED: _ClassVar[AuditLevel]
    AUDIT_LEVEL_ERRORS: _ClassVar[AuditLevel]
    AUDIT_LEVEL_ALL: _ClassVar[AuditLevel]

AUDIT_LEVEL_UNSPECIFIED: AuditLevel
AUDIT_LEVEL_ERRORS: AuditLevel
AUDIT_LEVEL_ALL: AuditLevel
AUDIT_FIELD_NUMBER: _ClassVar[int]
audit: _descriptor.FieldDescriptor

class AuditOptions(_message.Message):
    __slots__ = ("level", "description")
    LEVEL_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    level: AuditLevel
    description: str
    def __init__(
        self,
        level: _Optional[_Union[AuditLevel, str]] = ...,
        description: _Optional[str] = ...,
    ) -> None: ...
