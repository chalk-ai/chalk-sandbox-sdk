from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
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

class SecretSource(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SECRET_SOURCE_UNSPECIFIED: _ClassVar[SecretSource]
    SECRET_SOURCE_MANAGED: _ClassVar[SecretSource]
    SECRET_SOURCE_EXTERNAL: _ClassVar[SecretSource]

SECRET_SOURCE_UNSPECIFIED: SecretSource
SECRET_SOURCE_MANAGED: SecretSource
SECRET_SOURCE_EXTERNAL: SecretSource

class Secret(_message.Message):
    __slots__ = ("id", "name", "updated_at", "integration_id", "source")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    INTEGRATION_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    updated_at: _timestamp_pb2.Timestamp
    integration_id: str
    source: SecretSource
    def __init__(
        self,
        id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        updated_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        integration_id: _Optional[str] = ...,
        source: _Optional[_Union[SecretSource, str]] = ...,
    ) -> None: ...

class SecretValue(_message.Message):
    __slots__ = ("name", "value", "source")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    name: str
    value: str
    source: SecretSource
    def __init__(
        self,
        name: _Optional[str] = ...,
        value: _Optional[str] = ...,
        source: _Optional[_Union[SecretSource, str]] = ...,
    ) -> None: ...

class SecretConfigValue(_message.Message):
    __slots__ = ("literal", "secret_id")
    LITERAL_FIELD_NUMBER: _ClassVar[int]
    SECRET_ID_FIELD_NUMBER: _ClassVar[int]
    literal: str
    secret_id: str
    def __init__(self, literal: _Optional[str] = ..., secret_id: _Optional[str] = ...) -> None: ...

class SecretWithValue(_message.Message):
    __slots__ = ("id", "updated_at", "name", "full_name", "value", "source")
    ID_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    FULL_NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    id: str
    updated_at: _timestamp_pb2.Timestamp
    name: str
    full_name: str
    value: str
    source: SecretSource
    def __init__(
        self,
        id: _Optional[str] = ...,
        updated_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        name: _Optional[str] = ...,
        full_name: _Optional[str] = ...,
        value: _Optional[str] = ...,
        source: _Optional[_Union[SecretSource, str]] = ...,
    ) -> None: ...

class ListSecretsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListSecretsResponse(_message.Message):
    __slots__ = ("secrets",)
    SECRETS_FIELD_NUMBER: _ClassVar[int]
    secrets: _containers.RepeatedCompositeFieldContainer[Secret]
    def __init__(self, secrets: _Optional[_Iterable[_Union[Secret, _Mapping]]] = ...) -> None: ...

class GetSecretValueRequest(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class GetSecretValueResponse(_message.Message):
    __slots__ = ("secret_value",)
    SECRET_VALUE_FIELD_NUMBER: _ClassVar[int]
    secret_value: SecretValue
    def __init__(self, secret_value: _Optional[_Union[SecretValue, _Mapping]] = ...) -> None: ...

class UpsertSecretRequest(_message.Message):
    __slots__ = ("name", "value", "config")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    name: str
    value: str
    config: SecretConfigValue
    def __init__(
        self,
        name: _Optional[str] = ...,
        value: _Optional[str] = ...,
        config: _Optional[_Union[SecretConfigValue, _Mapping]] = ...,
    ) -> None: ...

class UpsertSecretResponse(_message.Message):
    __slots__ = ("secrets",)
    SECRETS_FIELD_NUMBER: _ClassVar[int]
    secrets: _containers.RepeatedCompositeFieldContainer[Secret]
    def __init__(self, secrets: _Optional[_Iterable[_Union[Secret, _Mapping]]] = ...) -> None: ...

class DeleteSecretRequest(_message.Message):
    __slots__ = ("name",)
    NAME_FIELD_NUMBER: _ClassVar[int]
    name: str
    def __init__(self, name: _Optional[str] = ...) -> None: ...

class DeleteSecretResponse(_message.Message):
    __slots__ = ("secrets",)
    SECRETS_FIELD_NUMBER: _ClassVar[int]
    secrets: _containers.RepeatedCompositeFieldContainer[Secret]
    def __init__(self, secrets: _Optional[_Iterable[_Union[Secret, _Mapping]]] = ...) -> None: ...

class GetAllSecretValuesRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetAllSecretValuesResponse(_message.Message):
    __slots__ = ("values",)
    class ValuesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

    VALUES_FIELD_NUMBER: _ClassVar[int]
    values: _containers.ScalarMap[str, str]
    def __init__(self, values: _Optional[_Mapping[str, str]] = ...) -> None: ...
