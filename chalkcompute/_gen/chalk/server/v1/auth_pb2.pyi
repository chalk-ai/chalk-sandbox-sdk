from chalkcompute._gen.chalk.auth.v1 import audit_pb2 as _audit_pb2
from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
from chalkcompute._gen.chalk.server.v1 import link_pb2 as _link_pb2
from chalkcompute._gen.chalk.utils.v1 import sensitive_pb2 as _sensitive_pb2
from google.protobuf import duration_pb2 as _duration_pb2
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

class AdapterUser(_message.Message):
    __slots__ = ("id", "email_verified", "team_id", "name", "email", "image")
    ID_FIELD_NUMBER: _ClassVar[int]
    EMAIL_VERIFIED_FIELD_NUMBER: _ClassVar[int]
    TEAM_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    EMAIL_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    id: str
    email_verified: _timestamp_pb2.Timestamp
    team_id: str
    name: str
    email: str
    image: str
    def __init__(
        self,
        id: _Optional[str] = ...,
        email_verified: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        team_id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        email: _Optional[str] = ...,
        image: _Optional[str] = ...,
    ) -> None: ...

class AdapterUserNoId(_message.Message):
    __slots__ = ("email_verified", "team_id", "name", "email", "image")
    EMAIL_VERIFIED_FIELD_NUMBER: _ClassVar[int]
    TEAM_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    EMAIL_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    email_verified: _timestamp_pb2.Timestamp
    team_id: str
    name: str
    email: str
    image: str
    def __init__(
        self,
        email_verified: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        team_id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        email: _Optional[str] = ...,
        image: _Optional[str] = ...,
    ) -> None: ...

class AdapterSession(_message.Message):
    __slots__ = ("id", "session_token", "user_id", "expires")
    ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_FIELD_NUMBER: _ClassVar[int]
    id: str
    session_token: str
    user_id: str
    expires: _timestamp_pb2.Timestamp
    def __init__(
        self,
        id: _Optional[str] = ...,
        session_token: _Optional[str] = ...,
        user_id: _Optional[str] = ...,
        expires: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
    ) -> None: ...

class AdapterCreateSession(_message.Message):
    __slots__ = ("session_token", "user_id", "expires")
    SESSION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_FIELD_NUMBER: _ClassVar[int]
    session_token: str
    user_id: str
    expires: _timestamp_pb2.Timestamp
    def __init__(
        self,
        session_token: _Optional[str] = ...,
        user_id: _Optional[str] = ...,
        expires: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
    ) -> None: ...

class UpdateAdapterSession(_message.Message):
    __slots__ = ("id", "session_token", "user_id", "expires")
    ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_FIELD_NUMBER: _ClassVar[int]
    id: str
    session_token: str
    user_id: str
    expires: _timestamp_pb2.Timestamp
    def __init__(
        self,
        id: _Optional[str] = ...,
        session_token: _Optional[str] = ...,
        user_id: _Optional[str] = ...,
        expires: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
    ) -> None: ...

class NextAccount(_message.Message):
    __slots__ = (
        "provider_account_id",
        "user_id",
        "provider",
        "type",
        "access_token",
        "expires_at",
        "scope",
        "token_type",
        "id_token",
        "refresh_token",
        "session_state",
        "oauth_token_secret",
        "oauth_token",
        "refresh_token_expires_in",
    )
    PROVIDER_ACCOUNT_ID_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    ACCESS_TOKEN_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    TOKEN_TYPE_FIELD_NUMBER: _ClassVar[int]
    ID_TOKEN_FIELD_NUMBER: _ClassVar[int]
    REFRESH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    SESSION_STATE_FIELD_NUMBER: _ClassVar[int]
    OAUTH_TOKEN_SECRET_FIELD_NUMBER: _ClassVar[int]
    OAUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    REFRESH_TOKEN_EXPIRES_IN_FIELD_NUMBER: _ClassVar[int]
    provider_account_id: str
    user_id: str
    provider: str
    type: str
    access_token: str
    expires_at: int
    scope: str
    token_type: str
    id_token: str
    refresh_token: str
    session_state: str
    oauth_token_secret: str
    oauth_token: str
    refresh_token_expires_in: int
    def __init__(
        self,
        provider_account_id: _Optional[str] = ...,
        user_id: _Optional[str] = ...,
        provider: _Optional[str] = ...,
        type: _Optional[str] = ...,
        access_token: _Optional[str] = ...,
        expires_at: _Optional[int] = ...,
        scope: _Optional[str] = ...,
        token_type: _Optional[str] = ...,
        id_token: _Optional[str] = ...,
        refresh_token: _Optional[str] = ...,
        session_state: _Optional[str] = ...,
        oauth_token_secret: _Optional[str] = ...,
        oauth_token: _Optional[str] = ...,
        refresh_token_expires_in: _Optional[int] = ...,
    ) -> None: ...

class VerificationToken(_message.Message):
    __slots__ = ("identifier", "expires", "token")
    IDENTIFIER_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_FIELD_NUMBER: _ClassVar[int]
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    identifier: str
    expires: _timestamp_pb2.Timestamp
    token: str
    def __init__(
        self,
        identifier: _Optional[str] = ...,
        expires: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        token: _Optional[str] = ...,
    ) -> None: ...

class CheckTeamInvitesRequest(_message.Message):
    __slots__ = ("user_id",)
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    user_id: str
    def __init__(self, user_id: _Optional[str] = ...) -> None: ...

class CheckTeamInvitesResponse(_message.Message):
    __slots__ = ("team_id",)
    TEAM_ID_FIELD_NUMBER: _ClassVar[int]
    team_id: str
    def __init__(self, team_id: _Optional[str] = ...) -> None: ...

class CreateUserRequest(_message.Message):
    __slots__ = ("user",)
    USER_FIELD_NUMBER: _ClassVar[int]
    user: AdapterUserNoId
    def __init__(self, user: _Optional[_Union[AdapterUserNoId, _Mapping]] = ...) -> None: ...

class GetUserByIdRequest(_message.Message):
    __slots__ = ("user_id",)
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    user_id: str
    def __init__(self, user_id: _Optional[str] = ...) -> None: ...

class GetUserByEmailRequest(_message.Message):
    __slots__ = ("email",)
    EMAIL_FIELD_NUMBER: _ClassVar[int]
    email: str
    def __init__(self, email: _Optional[str] = ...) -> None: ...

class GetUserByAccountRequest(_message.Message):
    __slots__ = ("provider_account_id", "provider")
    PROVIDER_ACCOUNT_ID_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    provider_account_id: str
    provider: str
    def __init__(self, provider_account_id: _Optional[str] = ..., provider: _Optional[str] = ...) -> None: ...

class UpdateUserFields(_message.Message):
    __slots__ = ("email_verified", "team_id", "name", "email", "image")
    EMAIL_VERIFIED_FIELD_NUMBER: _ClassVar[int]
    TEAM_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    EMAIL_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    email_verified: _timestamp_pb2.Timestamp
    team_id: str
    name: str
    email: str
    image: str
    def __init__(
        self,
        email_verified: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        team_id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        email: _Optional[str] = ...,
        image: _Optional[str] = ...,
    ) -> None: ...

class UpdateUserRequest(_message.Message):
    __slots__ = ("id", "fields")
    ID_FIELD_NUMBER: _ClassVar[int]
    FIELDS_FIELD_NUMBER: _ClassVar[int]
    id: str
    fields: UpdateUserFields
    def __init__(
        self, id: _Optional[str] = ..., fields: _Optional[_Union[UpdateUserFields, _Mapping]] = ...
    ) -> None: ...

class LinkAccountRequest(_message.Message):
    __slots__ = ("account",)
    ACCOUNT_FIELD_NUMBER: _ClassVar[int]
    account: NextAccount
    def __init__(self, account: _Optional[_Union[NextAccount, _Mapping]] = ...) -> None: ...

class CreateSessionRequest(_message.Message):
    __slots__ = ("session",)
    SESSION_FIELD_NUMBER: _ClassVar[int]
    session: AdapterCreateSession
    def __init__(self, session: _Optional[_Union[AdapterCreateSession, _Mapping]] = ...) -> None: ...

class GetSessionAndUserRequest(_message.Message):
    __slots__ = ("session_token",)
    SESSION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    session_token: str
    def __init__(self, session_token: _Optional[str] = ...) -> None: ...

class GetSessionAndUserResponse(_message.Message):
    __slots__ = ("session", "user")
    SESSION_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    session: AdapterSession
    user: AdapterUser
    def __init__(
        self,
        session: _Optional[_Union[AdapterSession, _Mapping]] = ...,
        user: _Optional[_Union[AdapterUser, _Mapping]] = ...,
    ) -> None: ...

class UpdateSessionRequest(_message.Message):
    __slots__ = ("session",)
    SESSION_FIELD_NUMBER: _ClassVar[int]
    session: UpdateAdapterSession
    def __init__(self, session: _Optional[_Union[UpdateAdapterSession, _Mapping]] = ...) -> None: ...

class DeleteSessionRequest(_message.Message):
    __slots__ = ("session_token",)
    SESSION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    session_token: str
    def __init__(self, session_token: _Optional[str] = ...) -> None: ...

class CreateVerificationTokenRequest(_message.Message):
    __slots__ = ("verification_token",)
    VERIFICATION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    verification_token: VerificationToken
    def __init__(self, verification_token: _Optional[_Union[VerificationToken, _Mapping]] = ...) -> None: ...

class UseVerificationTokenRequest(_message.Message):
    __slots__ = ("identifier", "token")
    IDENTIFIER_FIELD_NUMBER: _ClassVar[int]
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    identifier: str
    token: str
    def __init__(self, identifier: _Optional[str] = ..., token: _Optional[str] = ...) -> None: ...

class UpsertUserByEmailFields(_message.Message):
    __slots__ = ("email_verified", "name", "team_id")
    EMAIL_VERIFIED_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    TEAM_ID_FIELD_NUMBER: _ClassVar[int]
    email_verified: _timestamp_pb2.Timestamp
    name: str
    team_id: str
    def __init__(
        self,
        email_verified: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        name: _Optional[str] = ...,
        team_id: _Optional[str] = ...,
    ) -> None: ...

class UpsertUserByEmailRequest(_message.Message):
    __slots__ = ("email", "fields")
    EMAIL_FIELD_NUMBER: _ClassVar[int]
    FIELDS_FIELD_NUMBER: _ClassVar[int]
    email: str
    fields: UpsertUserByEmailFields
    def __init__(
        self, email: _Optional[str] = ..., fields: _Optional[_Union[UpsertUserByEmailFields, _Mapping]] = ...
    ) -> None: ...

class CreateUserResponse(_message.Message):
    __slots__ = ("user",)
    USER_FIELD_NUMBER: _ClassVar[int]
    user: AdapterUser
    def __init__(self, user: _Optional[_Union[AdapterUser, _Mapping]] = ...) -> None: ...

class GetUserByIdResponse(_message.Message):
    __slots__ = ("user",)
    USER_FIELD_NUMBER: _ClassVar[int]
    user: AdapterUser
    def __init__(self, user: _Optional[_Union[AdapterUser, _Mapping]] = ...) -> None: ...

class GetUserByEmailResponse(_message.Message):
    __slots__ = ("user",)
    USER_FIELD_NUMBER: _ClassVar[int]
    user: AdapterUser
    def __init__(self, user: _Optional[_Union[AdapterUser, _Mapping]] = ...) -> None: ...

class GetUserByAccountResponse(_message.Message):
    __slots__ = ("user",)
    USER_FIELD_NUMBER: _ClassVar[int]
    user: AdapterUser
    def __init__(self, user: _Optional[_Union[AdapterUser, _Mapping]] = ...) -> None: ...

class UpdateUserResponse(_message.Message):
    __slots__ = ("user",)
    USER_FIELD_NUMBER: _ClassVar[int]
    user: AdapterUser
    def __init__(self, user: _Optional[_Union[AdapterUser, _Mapping]] = ...) -> None: ...

class LinkAccountResponse(_message.Message):
    __slots__ = ("account",)
    ACCOUNT_FIELD_NUMBER: _ClassVar[int]
    account: NextAccount
    def __init__(self, account: _Optional[_Union[NextAccount, _Mapping]] = ...) -> None: ...

class CreateSessionResponse(_message.Message):
    __slots__ = ("session",)
    SESSION_FIELD_NUMBER: _ClassVar[int]
    session: AdapterSession
    def __init__(self, session: _Optional[_Union[AdapterSession, _Mapping]] = ...) -> None: ...

class UpdateSessionResponse(_message.Message):
    __slots__ = ("session",)
    SESSION_FIELD_NUMBER: _ClassVar[int]
    session: AdapterSession
    def __init__(self, session: _Optional[_Union[AdapterSession, _Mapping]] = ...) -> None: ...

class DeleteSessionResponse(_message.Message):
    __slots__ = ("session",)
    SESSION_FIELD_NUMBER: _ClassVar[int]
    session: AdapterSession
    def __init__(self, session: _Optional[_Union[AdapterSession, _Mapping]] = ...) -> None: ...

class CreateVerificationTokenResponse(_message.Message):
    __slots__ = ("verification_token",)
    VERIFICATION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    verification_token: VerificationToken
    def __init__(self, verification_token: _Optional[_Union[VerificationToken, _Mapping]] = ...) -> None: ...

class UseVerificationTokenResponse(_message.Message):
    __slots__ = ("verification_token",)
    VERIFICATION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    verification_token: VerificationToken
    def __init__(self, verification_token: _Optional[_Union[VerificationToken, _Mapping]] = ...) -> None: ...

class UpsertUserByEmailResponse(_message.Message):
    __slots__ = ("user",)
    USER_FIELD_NUMBER: _ClassVar[int]
    user: AdapterUser
    def __init__(self, user: _Optional[_Union[AdapterUser, _Mapping]] = ...) -> None: ...

class SelfServiceCreateTeamRequest(_message.Message):
    __slots__ = ("user_id", "team_name")
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    TEAM_NAME_FIELD_NUMBER: _ClassVar[int]
    user_id: str
    team_name: str
    def __init__(self, user_id: _Optional[str] = ..., team_name: _Optional[str] = ...) -> None: ...

class SelfServiceCreateTeamResponse(_message.Message):
    __slots__ = ("team_id", "project_id")
    TEAM_ID_FIELD_NUMBER: _ClassVar[int]
    PROJECT_ID_FIELD_NUMBER: _ClassVar[int]
    team_id: str
    project_id: str
    def __init__(self, team_id: _Optional[str] = ..., project_id: _Optional[str] = ...) -> None: ...

class GetTokenRequest(_message.Message):
    __slots__ = ("client_id", "client_secret", "grant_type", "scope", "expires_at")
    CLIENT_ID_FIELD_NUMBER: _ClassVar[int]
    CLIENT_SECRET_FIELD_NUMBER: _ClassVar[int]
    GRANT_TYPE_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    client_id: str
    client_secret: str
    grant_type: str
    scope: str
    expires_at: _duration_pb2.Duration
    def __init__(
        self,
        client_id: _Optional[str] = ...,
        client_secret: _Optional[str] = ...,
        grant_type: _Optional[str] = ...,
        scope: _Optional[str] = ...,
        expires_at: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ...,
    ) -> None: ...

class GetTokenResponse(_message.Message):
    __slots__ = (
        "access_token",
        "token_type",
        "expires_in",
        "expires_at",
        "api_server",
        "primary_environment",
        "engines",
        "grpc_engines",
        "environment_id_to_name",
    )
    class EnginesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

    class GrpcEnginesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

    class EnvironmentIdToNameEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

    ACCESS_TOKEN_FIELD_NUMBER: _ClassVar[int]
    TOKEN_TYPE_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_IN_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    API_SERVER_FIELD_NUMBER: _ClassVar[int]
    PRIMARY_ENVIRONMENT_FIELD_NUMBER: _ClassVar[int]
    ENGINES_FIELD_NUMBER: _ClassVar[int]
    GRPC_ENGINES_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_ID_TO_NAME_FIELD_NUMBER: _ClassVar[int]
    access_token: str
    token_type: str
    expires_in: int
    expires_at: _timestamp_pb2.Timestamp
    api_server: str
    primary_environment: str
    engines: _containers.ScalarMap[str, str]
    grpc_engines: _containers.ScalarMap[str, str]
    environment_id_to_name: _containers.ScalarMap[str, str]
    def __init__(
        self,
        access_token: _Optional[str] = ...,
        token_type: _Optional[str] = ...,
        expires_in: _Optional[int] = ...,
        expires_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        api_server: _Optional[str] = ...,
        primary_environment: _Optional[str] = ...,
        engines: _Optional[_Mapping[str, str]] = ...,
        grpc_engines: _Optional[_Mapping[str, str]] = ...,
        environment_id_to_name: _Optional[_Mapping[str, str]] = ...,
    ) -> None: ...

class UpdateLinkSessionRequest(_message.Message):
    __slots__ = ("status", "user_id", "session_id")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    status: str
    user_id: str
    session_id: str
    def __init__(
        self, status: _Optional[str] = ..., user_id: _Optional[str] = ..., session_id: _Optional[str] = ...
    ) -> None: ...

class UpdateLinkSessionResponse(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class GetProjectInfoRequest(_message.Message):
    __slots__ = ("project_id",)
    PROJECT_ID_FIELD_NUMBER: _ClassVar[int]
    project_id: str
    def __init__(self, project_id: _Optional[str] = ...) -> None: ...

class ProjectInfo(_message.Message):
    __slots__ = ("id", "name", "team_id")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    TEAM_ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    team_id: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., team_id: _Optional[str] = ...) -> None: ...

class GetProjectInfoResponse(_message.Message):
    __slots__ = ("project",)
    PROJECT_FIELD_NUMBER: _ClassVar[int]
    project: ProjectInfo
    def __init__(self, project: _Optional[_Union[ProjectInfo, _Mapping]] = ...) -> None: ...

class GetInternalWorkingTokenRequest(_message.Message):
    __slots__ = ("requested_permissions",)
    REQUESTED_PERMISSIONS_FIELD_NUMBER: _ClassVar[int]
    requested_permissions: _containers.RepeatedScalarFieldContainer[_permissions_pb2.Permission]
    def __init__(
        self, requested_permissions: _Optional[_Iterable[_Union[_permissions_pb2.Permission, str]]] = ...
    ) -> None: ...

class GetInternalWorkingTokenResponse(_message.Message):
    __slots__ = ("access_token", "expires_at")
    ACCESS_TOKEN_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    access_token: str
    expires_at: _timestamp_pb2.Timestamp
    def __init__(
        self,
        access_token: _Optional[str] = ...,
        expires_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
    ) -> None: ...
