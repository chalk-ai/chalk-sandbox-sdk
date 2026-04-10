from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FeaturePermission(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FEATURE_PERMISSION_UNSPECIFIED: _ClassVar[FeaturePermission]
    FEATURE_PERMISSION_ALLOW: _ClassVar[FeaturePermission]
    FEATURE_PERMISSION_ALLOW_INTERNAL: _ClassVar[FeaturePermission]
    FEATURE_PERMISSION_DENY: _ClassVar[FeaturePermission]

FEATURE_PERMISSION_UNSPECIFIED: FeaturePermission
FEATURE_PERMISSION_ALLOW: FeaturePermission
FEATURE_PERMISSION_ALLOW_INTERNAL: FeaturePermission
FEATURE_PERMISSION_DENY: FeaturePermission

class FeaturePermissions(_message.Message):
    __slots__ = ("tags", "default_permission")
    class TagsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: FeaturePermission
        def __init__(
            self, key: _Optional[str] = ..., value: _Optional[_Union[FeaturePermission, str]] = ...
        ) -> None: ...

    TAGS_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_PERMISSION_FIELD_NUMBER: _ClassVar[int]
    tags: _containers.ScalarMap[str, FeaturePermission]
    default_permission: FeaturePermission
    def __init__(
        self,
        tags: _Optional[_Mapping[str, FeaturePermission]] = ...,
        default_permission: _Optional[_Union[FeaturePermission, str]] = ...,
    ) -> None: ...
