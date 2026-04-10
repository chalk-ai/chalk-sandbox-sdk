from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
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

class ResourceLimits(_message.Message):
    __slots__ = ("cpu", "memory", "gpu")
    CPU_FIELD_NUMBER: _ClassVar[int]
    MEMORY_FIELD_NUMBER: _ClassVar[int]
    GPU_FIELD_NUMBER: _ClassVar[int]
    cpu: str
    memory: str
    gpu: str
    def __init__(self, cpu: _Optional[str] = ..., memory: _Optional[str] = ..., gpu: _Optional[str] = ...) -> None: ...

class VolumeMount(_message.Message):
    __slots__ = ("name", "mount_path", "type", "size_limit")
    NAME_FIELD_NUMBER: _ClassVar[int]
    MOUNT_PATH_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    SIZE_LIMIT_FIELD_NUMBER: _ClassVar[int]
    name: str
    mount_path: str
    type: str
    size_limit: str
    def __init__(
        self,
        name: _Optional[str] = ...,
        mount_path: _Optional[str] = ...,
        type: _Optional[str] = ...,
        size_limit: _Optional[str] = ...,
    ) -> None: ...

class ChalkContainerSpec(_message.Message):
    __slots__ = (
        "name",
        "image",
        "entrypoint",
        "tags",
        "port",
        "lifetime",
        "resources",
        "enable_ssh",
        "env_vars",
        "volumes",
        "protocol",
        "routing",
        "authentication",
    )
    class TagsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

    class EnvVarsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

    NAME_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    ENTRYPOINT_FIELD_NUMBER: _ClassVar[int]
    TAGS_FIELD_NUMBER: _ClassVar[int]
    PORT_FIELD_NUMBER: _ClassVar[int]
    LIFETIME_FIELD_NUMBER: _ClassVar[int]
    RESOURCES_FIELD_NUMBER: _ClassVar[int]
    ENABLE_SSH_FIELD_NUMBER: _ClassVar[int]
    ENV_VARS_FIELD_NUMBER: _ClassVar[int]
    VOLUMES_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    ROUTING_FIELD_NUMBER: _ClassVar[int]
    AUTHENTICATION_FIELD_NUMBER: _ClassVar[int]
    name: str
    image: str
    entrypoint: _containers.RepeatedScalarFieldContainer[str]
    tags: _containers.ScalarMap[str, str]
    port: int
    lifetime: _duration_pb2.Duration
    resources: ResourceLimits
    enable_ssh: bool
    env_vars: _containers.ScalarMap[str, str]
    volumes: _containers.RepeatedCompositeFieldContainer[VolumeMount]
    protocol: str
    routing: str
    authentication: str
    def __init__(
        self,
        name: _Optional[str] = ...,
        image: _Optional[str] = ...,
        entrypoint: _Optional[_Iterable[str]] = ...,
        tags: _Optional[_Mapping[str, str]] = ...,
        port: _Optional[int] = ...,
        lifetime: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ...,
        resources: _Optional[_Union[ResourceLimits, _Mapping]] = ...,
        enable_ssh: bool = ...,
        env_vars: _Optional[_Mapping[str, str]] = ...,
        volumes: _Optional[_Iterable[_Union[VolumeMount, _Mapping]]] = ...,
        protocol: _Optional[str] = ...,
        routing: _Optional[str] = ...,
        authentication: _Optional[str] = ...,
    ) -> None: ...

class ContainerRequest(_message.Message):
    __slots__ = ("spec",)
    SPEC_FIELD_NUMBER: _ClassVar[int]
    spec: ChalkContainerSpec
    def __init__(self, spec: _Optional[_Union[ChalkContainerSpec, _Mapping]] = ...) -> None: ...

class HealthCheck(_message.Message):
    __slots__ = ("healthy", "status_code", "error")
    HEALTHY_FIELD_NUMBER: _ClassVar[int]
    STATUS_CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    healthy: bool
    status_code: int
    error: str
    def __init__(self, healthy: bool = ..., status_code: _Optional[int] = ..., error: _Optional[str] = ...) -> None: ...

class ContainerResponse(_message.Message):
    __slots__ = (
        "id",
        "name",
        "status",
        "status_message",
        "spec",
        "created_at",
        "stopped_at",
        "pod_name",
        "web_url",
        "ssh_private_key",
        "ssh_username",
        "ssh_host",
        "ssh_port",
        "health_check",
    )
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    SPEC_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    STOPPED_AT_FIELD_NUMBER: _ClassVar[int]
    POD_NAME_FIELD_NUMBER: _ClassVar[int]
    WEB_URL_FIELD_NUMBER: _ClassVar[int]
    SSH_PRIVATE_KEY_FIELD_NUMBER: _ClassVar[int]
    SSH_USERNAME_FIELD_NUMBER: _ClassVar[int]
    SSH_HOST_FIELD_NUMBER: _ClassVar[int]
    SSH_PORT_FIELD_NUMBER: _ClassVar[int]
    HEALTH_CHECK_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    status: str
    status_message: str
    spec: ChalkContainerSpec
    created_at: _timestamp_pb2.Timestamp
    stopped_at: _timestamp_pb2.Timestamp
    pod_name: str
    web_url: str
    ssh_private_key: str
    ssh_username: str
    ssh_host: str
    ssh_port: int
    health_check: HealthCheck
    def __init__(
        self,
        id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        status: _Optional[str] = ...,
        status_message: _Optional[str] = ...,
        spec: _Optional[_Union[ChalkContainerSpec, _Mapping]] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        stopped_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        pod_name: _Optional[str] = ...,
        web_url: _Optional[str] = ...,
        ssh_private_key: _Optional[str] = ...,
        ssh_username: _Optional[str] = ...,
        ssh_host: _Optional[str] = ...,
        ssh_port: _Optional[int] = ...,
        health_check: _Optional[_Union[HealthCheck, _Mapping]] = ...,
    ) -> None: ...

class RunContainerRequest(_message.Message):
    __slots__ = ("container",)
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    container: ContainerRequest
    def __init__(self, container: _Optional[_Union[ContainerRequest, _Mapping]] = ...) -> None: ...

class RunContainerResponse(_message.Message):
    __slots__ = ("container",)
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    container: ContainerResponse
    def __init__(self, container: _Optional[_Union[ContainerResponse, _Mapping]] = ...) -> None: ...

class StopContainerRequest(_message.Message):
    __slots__ = ("id", "name", "grace_period_seconds")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    GRACE_PERIOD_SECONDS_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    grace_period_seconds: int
    def __init__(
        self, id: _Optional[str] = ..., name: _Optional[str] = ..., grace_period_seconds: _Optional[int] = ...
    ) -> None: ...

class StopContainerResponse(_message.Message):
    __slots__ = ("container",)
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    container: ContainerResponse
    def __init__(self, container: _Optional[_Union[ContainerResponse, _Mapping]] = ...) -> None: ...

class GetContainerRequest(_message.Message):
    __slots__ = ("id", "name")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class GetContainerResponse(_message.Message):
    __slots__ = ("container",)
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    container: ContainerResponse
    def __init__(self, container: _Optional[_Union[ContainerResponse, _Mapping]] = ...) -> None: ...

class ListContainersRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListContainersResponse(_message.Message):
    __slots__ = ("containers",)
    CONTAINERS_FIELD_NUMBER: _ClassVar[int]
    containers: _containers.RepeatedCompositeFieldContainer[ContainerResponse]
    def __init__(self, containers: _Optional[_Iterable[_Union[ContainerResponse, _Mapping]]] = ...) -> None: ...

class ExecCommandRequest(_message.Message):
    __slots__ = ("id", "name", "command", "timeout", "stdin")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    STDIN_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    command: _containers.RepeatedScalarFieldContainer[str]
    timeout: _duration_pb2.Duration
    stdin: bytes
    def __init__(
        self,
        id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        command: _Optional[_Iterable[str]] = ...,
        timeout: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ...,
        stdin: _Optional[bytes] = ...,
    ) -> None: ...

class ExecCommandResponse(_message.Message):
    __slots__ = ("stdout", "stderr", "exit_code")
    STDOUT_FIELD_NUMBER: _ClassVar[int]
    STDERR_FIELD_NUMBER: _ClassVar[int]
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    stdout: bytes
    stderr: bytes
    exit_code: int
    def __init__(
        self, stdout: _Optional[bytes] = ..., stderr: _Optional[bytes] = ..., exit_code: _Optional[int] = ...
    ) -> None: ...

class UpdateContainerStatusRequest(_message.Message):
    __slots__ = ("container_id", "status", "status_message")
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    container_id: str
    status: str
    status_message: str
    def __init__(
        self, container_id: _Optional[str] = ..., status: _Optional[str] = ..., status_message: _Optional[str] = ...
    ) -> None: ...

class UpdateContainerStatusResponse(_message.Message):
    __slots__ = ("container",)
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    container: ContainerResponse
    def __init__(self, container: _Optional[_Union[ContainerResponse, _Mapping]] = ...) -> None: ...

class BatchUpdateContainerStatusRequest(_message.Message):
    __slots__ = ("updates",)
    UPDATES_FIELD_NUMBER: _ClassVar[int]
    updates: _containers.RepeatedCompositeFieldContainer[UpdateContainerStatusRequest]
    def __init__(self, updates: _Optional[_Iterable[_Union[UpdateContainerStatusRequest, _Mapping]]] = ...) -> None: ...

class BatchUpdateContainerStatusResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GKEPodSnapshot(_message.Message):
    __slots__ = ("storage_bucket", "storage_path")
    STORAGE_BUCKET_FIELD_NUMBER: _ClassVar[int]
    STORAGE_PATH_FIELD_NUMBER: _ClassVar[int]
    storage_bucket: str
    storage_path: str
    def __init__(self, storage_bucket: _Optional[str] = ..., storage_path: _Optional[str] = ...) -> None: ...

class ContainerSnapshotSpec(_message.Message):
    __slots__ = ("gke_pod_snapshot",)
    GKE_POD_SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    gke_pod_snapshot: GKEPodSnapshot
    def __init__(self, gke_pod_snapshot: _Optional[_Union[GKEPodSnapshot, _Mapping]] = ...) -> None: ...

class ContainerSnapshot(_message.Message):
    __slots__ = (
        "id",
        "source_container_id",
        "container_spec",
        "snapshot_spec",
        "status",
        "status_message",
        "created_at",
        "completed_at",
        "created_by",
    )
    ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    CONTAINER_SPEC_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_SPEC_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_FIELD_NUMBER: _ClassVar[int]
    CREATED_BY_FIELD_NUMBER: _ClassVar[int]
    id: str
    source_container_id: str
    container_spec: ChalkContainerSpec
    snapshot_spec: ContainerSnapshotSpec
    status: str
    status_message: str
    created_at: _timestamp_pb2.Timestamp
    completed_at: _timestamp_pb2.Timestamp
    created_by: str
    def __init__(
        self,
        id: _Optional[str] = ...,
        source_container_id: _Optional[str] = ...,
        container_spec: _Optional[_Union[ChalkContainerSpec, _Mapping]] = ...,
        snapshot_spec: _Optional[_Union[ContainerSnapshotSpec, _Mapping]] = ...,
        status: _Optional[str] = ...,
        status_message: _Optional[str] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        completed_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        created_by: _Optional[str] = ...,
    ) -> None: ...

class SnapshotContainerRequest(_message.Message):
    __slots__ = ("id", "name")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class SnapshotContainerResponse(_message.Message):
    __slots__ = ("snapshot",)
    SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    snapshot: ContainerSnapshot
    def __init__(self, snapshot: _Optional[_Union[ContainerSnapshot, _Mapping]] = ...) -> None: ...

class GetContainerSnapshotRequest(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class GetContainerSnapshotResponse(_message.Message):
    __slots__ = ("snapshot",)
    SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    snapshot: ContainerSnapshot
    def __init__(self, snapshot: _Optional[_Union[ContainerSnapshot, _Mapping]] = ...) -> None: ...

class ListContainerSnapshotsRequest(_message.Message):
    __slots__ = ("source_container_id",)
    SOURCE_CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    source_container_id: str
    def __init__(self, source_container_id: _Optional[str] = ...) -> None: ...

class ListContainerSnapshotsResponse(_message.Message):
    __slots__ = ("snapshots",)
    SNAPSHOTS_FIELD_NUMBER: _ClassVar[int]
    snapshots: _containers.RepeatedCompositeFieldContainer[ContainerSnapshot]
    def __init__(self, snapshots: _Optional[_Iterable[_Union[ContainerSnapshot, _Mapping]]] = ...) -> None: ...
