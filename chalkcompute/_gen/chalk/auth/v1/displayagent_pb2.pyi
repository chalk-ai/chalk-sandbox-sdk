from chalkcompute._gen.chalk.auth.v1 import featurepermission_pb2 as _featurepermission_pb2
from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
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

class DisplayPermission(_message.Message):
    __slots__ = ("name", "permission")
    NAME_FIELD_NUMBER: _ClassVar[int]
    PERMISSION_FIELD_NUMBER: _ClassVar[int]
    name: str
    permission: _permissions_pb2.Permission
    def __init__(
        self, name: _Optional[str] = ..., permission: _Optional[_Union[_permissions_pb2.Permission, str]] = ...
    ) -> None: ...

class DisplayEnvironmentPermissions(_message.Message):
    __slots__ = ("environment_id", "environment_name", "project_id", "project_name", "permissions")
    ENVIRONMENT_ID_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_NAME_FIELD_NUMBER: _ClassVar[int]
    PROJECT_ID_FIELD_NUMBER: _ClassVar[int]
    PROJECT_NAME_FIELD_NUMBER: _ClassVar[int]
    PERMISSIONS_FIELD_NUMBER: _ClassVar[int]
    environment_id: str
    environment_name: str
    project_id: str
    project_name: str
    permissions: _containers.RepeatedCompositeFieldContainer[DisplayPermission]
    def __init__(
        self,
        environment_id: _Optional[str] = ...,
        environment_name: _Optional[str] = ...,
        project_id: _Optional[str] = ...,
        project_name: _Optional[str] = ...,
        permissions: _Optional[_Iterable[_Union[DisplayPermission, _Mapping]]] = ...,
    ) -> None: ...

class DisplayUserAgent(_message.Message):
    __slots__ = (
        "client_id",
        "user_id",
        "team_id",
        "permissions_by_environment",
        "impersonated",
        "name",
        "email",
        "team_name",
        "customer_claims",
    )
    CLIENT_ID_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    TEAM_ID_FIELD_NUMBER: _ClassVar[int]
    PERMISSIONS_BY_ENVIRONMENT_FIELD_NUMBER: _ClassVar[int]
    IMPERSONATED_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    EMAIL_FIELD_NUMBER: _ClassVar[int]
    TEAM_NAME_FIELD_NUMBER: _ClassVar[int]
    CUSTOMER_CLAIMS_FIELD_NUMBER: _ClassVar[int]
    client_id: str
    user_id: str
    team_id: str
    permissions_by_environment: _containers.RepeatedCompositeFieldContainer[DisplayEnvironmentPermissions]
    impersonated: bool
    name: str
    email: str
    team_name: str
    customer_claims: _containers.RepeatedCompositeFieldContainer[DisplayCustomClaim]
    def __init__(
        self,
        client_id: _Optional[str] = ...,
        user_id: _Optional[str] = ...,
        team_id: _Optional[str] = ...,
        permissions_by_environment: _Optional[_Iterable[_Union[DisplayEnvironmentPermissions, _Mapping]]] = ...,
        impersonated: bool = ...,
        name: _Optional[str] = ...,
        email: _Optional[str] = ...,
        team_name: _Optional[str] = ...,
        customer_claims: _Optional[_Iterable[_Union[DisplayCustomClaim, _Mapping]]] = ...,
    ) -> None: ...

class DisplayCustomClaim(_message.Message):
    __slots__ = ("key", "values")
    KEY_FIELD_NUMBER: _ClassVar[int]
    VALUES_FIELD_NUMBER: _ClassVar[int]
    key: str
    values: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, key: _Optional[str] = ..., values: _Optional[_Iterable[str]] = ...) -> None: ...

class DisplayServiceTokenAgent(_message.Message):
    __slots__ = (
        "id",
        "client_id",
        "team_id",
        "project_id",
        "environment_id",
        "permissions",
        "name",
        "team_name",
        "project_name",
        "environment_name",
        "customer_claims",
        "feature_permissions",
        "created_at",
        "team_permissions",
    )
    ID_FIELD_NUMBER: _ClassVar[int]
    CLIENT_ID_FIELD_NUMBER: _ClassVar[int]
    TEAM_ID_FIELD_NUMBER: _ClassVar[int]
    PROJECT_ID_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_ID_FIELD_NUMBER: _ClassVar[int]
    PERMISSIONS_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    TEAM_NAME_FIELD_NUMBER: _ClassVar[int]
    PROJECT_NAME_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_NAME_FIELD_NUMBER: _ClassVar[int]
    CUSTOMER_CLAIMS_FIELD_NUMBER: _ClassVar[int]
    FEATURE_PERMISSIONS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    TEAM_PERMISSIONS_FIELD_NUMBER: _ClassVar[int]
    id: str
    client_id: str
    team_id: str
    project_id: str
    environment_id: str
    permissions: _containers.RepeatedCompositeFieldContainer[DisplayPermission]
    name: str
    team_name: str
    project_name: str
    environment_name: str
    customer_claims: _containers.RepeatedCompositeFieldContainer[DisplayCustomClaim]
    feature_permissions: _featurepermission_pb2.FeaturePermissions
    created_at: _timestamp_pb2.Timestamp
    team_permissions: _containers.RepeatedCompositeFieldContainer[DisplayPermission]
    def __init__(
        self,
        id: _Optional[str] = ...,
        client_id: _Optional[str] = ...,
        team_id: _Optional[str] = ...,
        project_id: _Optional[str] = ...,
        environment_id: _Optional[str] = ...,
        permissions: _Optional[_Iterable[_Union[DisplayPermission, _Mapping]]] = ...,
        name: _Optional[str] = ...,
        team_name: _Optional[str] = ...,
        project_name: _Optional[str] = ...,
        environment_name: _Optional[str] = ...,
        customer_claims: _Optional[_Iterable[_Union[DisplayCustomClaim, _Mapping]]] = ...,
        feature_permissions: _Optional[_Union[_featurepermission_pb2.FeaturePermissions, _Mapping]] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        team_permissions: _Optional[_Iterable[_Union[DisplayPermission, _Mapping]]] = ...,
    ) -> None: ...

class DisplayEngineAgent(_message.Message):
    __slots__ = (
        "id",
        "team_id",
        "project_id",
        "environment_id",
        "impersonated",
        "team_name",
        "project_name",
        "environment_name",
    )
    ID_FIELD_NUMBER: _ClassVar[int]
    TEAM_ID_FIELD_NUMBER: _ClassVar[int]
    PROJECT_ID_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_ID_FIELD_NUMBER: _ClassVar[int]
    IMPERSONATED_FIELD_NUMBER: _ClassVar[int]
    TEAM_NAME_FIELD_NUMBER: _ClassVar[int]
    PROJECT_NAME_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_NAME_FIELD_NUMBER: _ClassVar[int]
    id: str
    team_id: str
    project_id: str
    environment_id: str
    impersonated: bool
    team_name: str
    project_name: str
    environment_name: str
    def __init__(
        self,
        id: _Optional[str] = ...,
        team_id: _Optional[str] = ...,
        project_id: _Optional[str] = ...,
        environment_id: _Optional[str] = ...,
        impersonated: bool = ...,
        team_name: _Optional[str] = ...,
        project_name: _Optional[str] = ...,
        environment_name: _Optional[str] = ...,
    ) -> None: ...

class DisplayTenantAgent(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class DisplayMetadataServiceAgent(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class DisplayAgent(_message.Message):
    __slots__ = ("user_agent", "service_token_agent", "engine_agent", "tenant_agent", "metadata_service_agent")
    USER_AGENT_FIELD_NUMBER: _ClassVar[int]
    SERVICE_TOKEN_AGENT_FIELD_NUMBER: _ClassVar[int]
    ENGINE_AGENT_FIELD_NUMBER: _ClassVar[int]
    TENANT_AGENT_FIELD_NUMBER: _ClassVar[int]
    METADATA_SERVICE_AGENT_FIELD_NUMBER: _ClassVar[int]
    user_agent: DisplayUserAgent
    service_token_agent: DisplayServiceTokenAgent
    engine_agent: DisplayEngineAgent
    tenant_agent: DisplayTenantAgent
    metadata_service_agent: DisplayMetadataServiceAgent
    def __init__(
        self,
        user_agent: _Optional[_Union[DisplayUserAgent, _Mapping]] = ...,
        service_token_agent: _Optional[_Union[DisplayServiceTokenAgent, _Mapping]] = ...,
        engine_agent: _Optional[_Union[DisplayEngineAgent, _Mapping]] = ...,
        tenant_agent: _Optional[_Union[DisplayTenantAgent, _Mapping]] = ...,
        metadata_service_agent: _Optional[_Union[DisplayMetadataServiceAgent, _Mapping]] = ...,
    ) -> None: ...
