from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import (
    ClassVar as _ClassVar,
    Iterable as _Iterable,
    Mapping as _Mapping,
    Optional as _Optional,
    Union as _Union,
)

DESCRIPTOR: _descriptor.FileDescriptor

class FieldChange(_message.Message):
    __slots__ = ("field_name", "previous_value", "new_value", "nested_changes")
    FIELD_NAME_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_VALUE_FIELD_NUMBER: _ClassVar[int]
    NEW_VALUE_FIELD_NUMBER: _ClassVar[int]
    NESTED_CHANGES_FIELD_NUMBER: _ClassVar[int]
    field_name: str
    previous_value: str
    new_value: str
    nested_changes: _containers.RepeatedCompositeFieldContainer[NestedChange]
    def __init__(
        self,
        field_name: _Optional[str] = ...,
        previous_value: _Optional[str] = ...,
        new_value: _Optional[str] = ...,
        nested_changes: _Optional[_Iterable[_Union[NestedChange, _Mapping]]] = ...,
    ) -> None: ...

class NestedChange(_message.Message):
    __slots__ = ("path", "previous_value", "new_value")
    PATH_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_VALUE_FIELD_NUMBER: _ClassVar[int]
    NEW_VALUE_FIELD_NUMBER: _ClassVar[int]
    path: str
    previous_value: str
    new_value: str
    def __init__(
        self, path: _Optional[str] = ..., previous_value: _Optional[str] = ..., new_value: _Optional[str] = ...
    ) -> None: ...
