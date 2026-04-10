from chalkcompute._gen.chalk.utils.v1 import encoding_pb2 as _encoding_pb2
from google.protobuf import descriptor_pb2 as _descriptor_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from typing import ClassVar as _ClassVar

DESCRIPTOR: _descriptor.FileDescriptor

class Permission(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PERMISSION_UNSPECIFIED: _ClassVar[Permission]
    PERMISSION_INSECURE_UNAUTHENTICATED: _ClassVar[Permission]
    PERMISSION_AUTHENTICATED: _ClassVar[Permission]
    PERMISSION_QUERY_ONLINE: _ClassVar[Permission]
    PERMISSION_QUERY_OFFLINE: _ClassVar[Permission]
    PERMISSION_MONITORING_CREATE: _ClassVar[Permission]
    PERMISSION_MONITORING_READ: _ClassVar[Permission]
    PERMISSION_TEAM_ADD: _ClassVar[Permission]
    PERMISSION_TEAM_DELETE: _ClassVar[Permission]
    PERMISSION_TEAM_LIST: _ClassVar[Permission]
    PERMISSION_TEAM_ADMIN: _ClassVar[Permission]
    PERMISSION_DEPLOY_READ: _ClassVar[Permission]
    PERMISSION_DEPLOY_CREATE: _ClassVar[Permission]
    PERMISSION_DEPLOY_PREVIEW: _ClassVar[Permission]
    PERMISSION_DEPLOY_REDEPLOY: _ClassVar[Permission]
    PERMISSION_LOGS_LIST: _ClassVar[Permission]
    PERMISSION_CRON_READ: _ClassVar[Permission]
    PERMISSION_CRON_CREATE: _ClassVar[Permission]
    PERMISSION_SECRETS_WRITE: _ClassVar[Permission]
    PERMISSION_SECRETS_DECRYPT: _ClassVar[Permission]
    PERMISSION_SECRETS_LIST: _ClassVar[Permission]
    PERMISSION_TOKENS_WRITE: _ClassVar[Permission]
    PERMISSION_TOKENS_LIST: _ClassVar[Permission]
    PERMISSION_MIGRATE_READ: _ClassVar[Permission]
    PERMISSION_MIGRATE_PLAN: _ClassVar[Permission]
    PERMISSION_MIGRATE_EXECUTE: _ClassVar[Permission]
    PERMISSION_PROJECT_CREATE: _ClassVar[Permission]
    PERMISSION_CHALK_ADMIN: _ClassVar[Permission]
    PERMISSION_BILLING_READ: _ClassVar[Permission]
    PERMISSION_AUTH_SERVICE_MANAGER: _ClassVar[Permission]
    PERMISSION_INFRASTRUCTURE_READ: _ClassVar[Permission]
    PERMISSION_INFRASTRUCTURE_WRITE: _ClassVar[Permission]
    PERMISSION_ENVIRONMENT_CREATE: _ClassVar[Permission]
    PERMISSION_INTERNAL_DATAPLANE_STATUS_UPDATE: _ClassVar[Permission]

PERMISSION_UNSPECIFIED: Permission
PERMISSION_INSECURE_UNAUTHENTICATED: Permission
PERMISSION_AUTHENTICATED: Permission
PERMISSION_QUERY_ONLINE: Permission
PERMISSION_QUERY_OFFLINE: Permission
PERMISSION_MONITORING_CREATE: Permission
PERMISSION_MONITORING_READ: Permission
PERMISSION_TEAM_ADD: Permission
PERMISSION_TEAM_DELETE: Permission
PERMISSION_TEAM_LIST: Permission
PERMISSION_TEAM_ADMIN: Permission
PERMISSION_DEPLOY_READ: Permission
PERMISSION_DEPLOY_CREATE: Permission
PERMISSION_DEPLOY_PREVIEW: Permission
PERMISSION_DEPLOY_REDEPLOY: Permission
PERMISSION_LOGS_LIST: Permission
PERMISSION_CRON_READ: Permission
PERMISSION_CRON_CREATE: Permission
PERMISSION_SECRETS_WRITE: Permission
PERMISSION_SECRETS_DECRYPT: Permission
PERMISSION_SECRETS_LIST: Permission
PERMISSION_TOKENS_WRITE: Permission
PERMISSION_TOKENS_LIST: Permission
PERMISSION_MIGRATE_READ: Permission
PERMISSION_MIGRATE_PLAN: Permission
PERMISSION_MIGRATE_EXECUTE: Permission
PERMISSION_PROJECT_CREATE: Permission
PERMISSION_CHALK_ADMIN: Permission
PERMISSION_BILLING_READ: Permission
PERMISSION_AUTH_SERVICE_MANAGER: Permission
PERMISSION_INFRASTRUCTURE_READ: Permission
PERMISSION_INFRASTRUCTURE_WRITE: Permission
PERMISSION_ENVIRONMENT_CREATE: Permission
PERMISSION_INTERNAL_DATAPLANE_STATUS_UPDATE: Permission
DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
description: _descriptor.FieldDescriptor
SLUG_FIELD_NUMBER: _ClassVar[int]
slug: _descriptor.FieldDescriptor
PERMISSION_FIELD_NUMBER: _ClassVar[int]
permission: _descriptor.FieldDescriptor
TEAM_PERMISSION_FIELD_NUMBER: _ClassVar[int]
team_permission: _descriptor.FieldDescriptor
