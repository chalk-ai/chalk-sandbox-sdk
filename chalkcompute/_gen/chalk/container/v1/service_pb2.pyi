from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
from chalkcompute._gen.chalk.flags.v1 import flags_pb2 as _flags_pb2
from google.protobuf import duration_pb2 as _duration_pb2
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

class ComputeClass(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    COMPUTE_CLASS_UNSPECIFIED: _ClassVar[ComputeClass]
    COMPUTE_CLASS_K8S: _ClassVar[ComputeClass]
    COMPUTE_CLASS_HOST: _ClassVar[ComputeClass]

class KernelPolicy(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    KERNEL_POLICY_UNSPECIFIED: _ClassVar[KernelPolicy]
    KERNEL_POLICY_RESTRICTED: _ClassVar[KernelPolicy]
    KERNEL_POLICY_OPEN: _ClassVar[KernelPolicy]

class ProcessState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PROCESS_STATE_UNSPECIFIED: _ClassVar[ProcessState]
    PROCESS_STATE_RUNNING: _ClassVar[ProcessState]
    PROCESS_STATE_EXITED: _ClassVar[ProcessState]
    PROCESS_STATE_FAILED: _ClassVar[ProcessState]
    PROCESS_STATE_TIMED_OUT: _ClassVar[ProcessState]

COMPUTE_CLASS_UNSPECIFIED: ComputeClass
COMPUTE_CLASS_K8S: ComputeClass
COMPUTE_CLASS_HOST: ComputeClass
KERNEL_POLICY_UNSPECIFIED: KernelPolicy
KERNEL_POLICY_RESTRICTED: KernelPolicy
KERNEL_POLICY_OPEN: KernelPolicy
PROCESS_STATE_UNSPECIFIED: ProcessState
PROCESS_STATE_RUNNING: ProcessState
PROCESS_STATE_EXITED: ProcessState
PROCESS_STATE_FAILED: ProcessState
PROCESS_STATE_TIMED_OUT: ProcessState

class ResourceLimits(_message.Message):
    __slots__ = ("cpu", "memory", "gpu")
    CPU_FIELD_NUMBER: _ClassVar[int]
    MEMORY_FIELD_NUMBER: _ClassVar[int]
    GPU_FIELD_NUMBER: _ClassVar[int]
    cpu: str
    memory: str
    gpu: str
    def __init__(
        self,
        cpu: _Optional[str] = ...,
        memory: _Optional[str] = ...,
        gpu: _Optional[str] = ...,
    ) -> None: ...

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

class SecretRef(_message.Message):
    __slots__ = ("integration_name", "secret_name", "keys", "aliases", "prefix")
    class AliasesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(
            self, key: _Optional[str] = ..., value: _Optional[str] = ...
        ) -> None: ...

    INTEGRATION_NAME_FIELD_NUMBER: _ClassVar[int]
    SECRET_NAME_FIELD_NUMBER: _ClassVar[int]
    KEYS_FIELD_NUMBER: _ClassVar[int]
    ALIASES_FIELD_NUMBER: _ClassVar[int]
    PREFIX_FIELD_NUMBER: _ClassVar[int]
    integration_name: str
    secret_name: str
    keys: _containers.RepeatedScalarFieldContainer[str]
    aliases: _containers.ScalarMap[str, str]
    prefix: str
    def __init__(
        self,
        integration_name: _Optional[str] = ...,
        secret_name: _Optional[str] = ...,
        keys: _Optional[_Iterable[str]] = ...,
        aliases: _Optional[_Mapping[str, str]] = ...,
        prefix: _Optional[str] = ...,
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
        "secret_refs",
        "security_policy",
        "network_policy",
        "compute_class",
    )
    class TagsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(
            self, key: _Optional[str] = ..., value: _Optional[str] = ...
        ) -> None: ...

    class EnvVarsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(
            self, key: _Optional[str] = ..., value: _Optional[str] = ...
        ) -> None: ...

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
    SECRET_REFS_FIELD_NUMBER: _ClassVar[int]
    SECURITY_POLICY_FIELD_NUMBER: _ClassVar[int]
    NETWORK_POLICY_FIELD_NUMBER: _ClassVar[int]
    COMPUTE_CLASS_FIELD_NUMBER: _ClassVar[int]
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
    secret_refs: _containers.RepeatedCompositeFieldContainer[SecretRef]
    security_policy: ContainerSecurityPolicy
    network_policy: NetworkPolicy
    compute_class: ComputeClass
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
        secret_refs: _Optional[_Iterable[_Union[SecretRef, _Mapping]]] = ...,
        security_policy: _Optional[_Union[ContainerSecurityPolicy, _Mapping]] = ...,
        network_policy: _Optional[_Union[NetworkPolicy, _Mapping]] = ...,
        compute_class: _Optional[_Union[ComputeClass, str]] = ...,
    ) -> None: ...

class ContainerSecurityPolicy(_message.Message):
    __slots__ = ("kernel_policy",)
    KERNEL_POLICY_FIELD_NUMBER: _ClassVar[int]
    kernel_policy: KernelPolicy
    def __init__(
        self, kernel_policy: _Optional[_Union[KernelPolicy, str]] = ...
    ) -> None: ...

class NetworkPolicy(_message.Message):
    __slots__ = ("allowed_routes",)
    ALLOWED_ROUTES_FIELD_NUMBER: _ClassVar[int]
    allowed_routes: _containers.RepeatedCompositeFieldContainer[AllowedRoute]
    def __init__(
        self, allowed_routes: _Optional[_Iterable[_Union[AllowedRoute, _Mapping]]] = ...
    ) -> None: ...

class AllowedRoute(_message.Message):
    __slots__ = ("route", "port_ranges")
    ROUTE_FIELD_NUMBER: _ClassVar[int]
    PORT_RANGES_FIELD_NUMBER: _ClassVar[int]
    route: str
    port_ranges: _containers.RepeatedCompositeFieldContainer[PortRange]
    def __init__(
        self,
        route: _Optional[str] = ...,
        port_ranges: _Optional[_Iterable[_Union[PortRange, _Mapping]]] = ...,
    ) -> None: ...

class PortRange(_message.Message):
    __slots__ = ("start_port", "end_port")
    START_PORT_FIELD_NUMBER: _ClassVar[int]
    END_PORT_FIELD_NUMBER: _ClassVar[int]
    start_port: int
    end_port: int
    def __init__(
        self, start_port: _Optional[int] = ..., end_port: _Optional[int] = ...
    ) -> None: ...

class ContainerRequest(_message.Message):
    __slots__ = ("spec",)
    SPEC_FIELD_NUMBER: _ClassVar[int]
    spec: ChalkContainerSpec
    def __init__(
        self, spec: _Optional[_Union[ChalkContainerSpec, _Mapping]] = ...
    ) -> None: ...

class HealthCheck(_message.Message):
    __slots__ = ("healthy", "status_code", "error")
    HEALTHY_FIELD_NUMBER: _ClassVar[int]
    STATUS_CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    healthy: bool
    status_code: int
    error: str
    def __init__(
        self,
        healthy: bool = ...,
        status_code: _Optional[int] = ...,
        error: _Optional[str] = ...,
    ) -> None: ...

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
    def __init__(
        self, container: _Optional[_Union[ContainerRequest, _Mapping]] = ...
    ) -> None: ...

class RunContainerResponse(_message.Message):
    __slots__ = ("container",)
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    container: ContainerResponse
    def __init__(
        self, container: _Optional[_Union[ContainerResponse, _Mapping]] = ...
    ) -> None: ...

class StopContainerRequest(_message.Message):
    __slots__ = ("id", "name", "grace_period_seconds")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    GRACE_PERIOD_SECONDS_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    grace_period_seconds: int
    def __init__(
        self,
        id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        grace_period_seconds: _Optional[int] = ...,
    ) -> None: ...

class StopContainerResponse(_message.Message):
    __slots__ = ("container",)
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    container: ContainerResponse
    def __init__(
        self, container: _Optional[_Union[ContainerResponse, _Mapping]] = ...
    ) -> None: ...

class GetContainerRequest(_message.Message):
    __slots__ = ("id", "name")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    def __init__(
        self, id: _Optional[str] = ..., name: _Optional[str] = ...
    ) -> None: ...

class GetContainerResponse(_message.Message):
    __slots__ = ("container",)
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    container: ContainerResponse
    def __init__(
        self, container: _Optional[_Union[ContainerResponse, _Mapping]] = ...
    ) -> None: ...

class ListContainersRequest(_message.Message):
    __slots__ = ("cursor", "limit")
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    cursor: str
    limit: int
    def __init__(
        self, cursor: _Optional[str] = ..., limit: _Optional[int] = ...
    ) -> None: ...

class ListContainersResponse(_message.Message):
    __slots__ = ("containers", "next_cursor")
    CONTAINERS_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    containers: _containers.RepeatedCompositeFieldContainer[ContainerResponse]
    next_cursor: str
    def __init__(
        self,
        containers: _Optional[_Iterable[_Union[ContainerResponse, _Mapping]]] = ...,
        next_cursor: _Optional[str] = ...,
    ) -> None: ...

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
        self,
        stdout: _Optional[bytes] = ...,
        stderr: _Optional[bytes] = ...,
        exit_code: _Optional[int] = ...,
    ) -> None: ...

class SessionRequest(_message.Message):
    __slots__ = (
        "new_process",
        "attach_session",
        "detach_session",
        "stdin_data",
        "stdin_eof",
        "signal",
        "pty_info",
        "get_process_status",
    )
    NEW_PROCESS_FIELD_NUMBER: _ClassVar[int]
    ATTACH_SESSION_FIELD_NUMBER: _ClassVar[int]
    DETACH_SESSION_FIELD_NUMBER: _ClassVar[int]
    STDIN_DATA_FIELD_NUMBER: _ClassVar[int]
    STDIN_EOF_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    PTY_INFO_FIELD_NUMBER: _ClassVar[int]
    GET_PROCESS_STATUS_FIELD_NUMBER: _ClassVar[int]
    new_process: NewProcess
    attach_session: AttachSession
    detach_session: DetachSession
    stdin_data: StdinData
    stdin_eof: StdinEof
    signal: SessionSignal
    pty_info: PtyInfo
    get_process_status: GetProcessStatus
    def __init__(
        self,
        new_process: _Optional[_Union[NewProcess, _Mapping]] = ...,
        attach_session: _Optional[_Union[AttachSession, _Mapping]] = ...,
        detach_session: _Optional[_Union[DetachSession, _Mapping]] = ...,
        stdin_data: _Optional[_Union[StdinData, _Mapping]] = ...,
        stdin_eof: _Optional[_Union[StdinEof, _Mapping]] = ...,
        signal: _Optional[_Union[SessionSignal, _Mapping]] = ...,
        pty_info: _Optional[_Union[PtyInfo, _Mapping]] = ...,
        get_process_status: _Optional[_Union[GetProcessStatus, _Mapping]] = ...,
    ) -> None: ...

class SessionResponse(_message.Message):
    __slots__ = (
        "error",
        "session_attached",
        "output_data",
        "process_status",
        "process_exited",
        "session_detached",
        "process_failed",
        "process_timed_out",
    )
    ERROR_FIELD_NUMBER: _ClassVar[int]
    SESSION_ATTACHED_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_DATA_FIELD_NUMBER: _ClassVar[int]
    PROCESS_STATUS_FIELD_NUMBER: _ClassVar[int]
    PROCESS_EXITED_FIELD_NUMBER: _ClassVar[int]
    SESSION_DETACHED_FIELD_NUMBER: _ClassVar[int]
    PROCESS_FAILED_FIELD_NUMBER: _ClassVar[int]
    PROCESS_TIMED_OUT_FIELD_NUMBER: _ClassVar[int]
    error: SessionError
    session_attached: SessionAttached
    output_data: OutputData
    process_status: ProcessStatus
    process_exited: ProcessExited
    session_detached: SessionDetached
    process_failed: ProcessFailed
    process_timed_out: ProcessTimedOut
    def __init__(
        self,
        error: _Optional[_Union[SessionError, _Mapping]] = ...,
        session_attached: _Optional[_Union[SessionAttached, _Mapping]] = ...,
        output_data: _Optional[_Union[OutputData, _Mapping]] = ...,
        process_status: _Optional[_Union[ProcessStatus, _Mapping]] = ...,
        process_exited: _Optional[_Union[ProcessExited, _Mapping]] = ...,
        session_detached: _Optional[_Union[SessionDetached, _Mapping]] = ...,
        process_failed: _Optional[_Union[ProcessFailed, _Mapping]] = ...,
        process_timed_out: _Optional[_Union[ProcessTimedOut, _Mapping]] = ...,
    ) -> None: ...

class SessionError(_message.Message):
    __slots__ = ("code", "message")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    code: str
    message: str
    def __init__(
        self, code: _Optional[str] = ..., message: _Optional[str] = ...
    ) -> None: ...

class NewProcess(_message.Message):
    __slots__ = (
        "container_id",
        "command",
        "args",
        "workdir",
        "env",
        "timeout_secs",
        "pty_info",
    )
    class EnvEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(
            self, key: _Optional[str] = ..., value: _Optional[str] = ...
        ) -> None: ...

    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    WORKDIR_FIELD_NUMBER: _ClassVar[int]
    ENV_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECS_FIELD_NUMBER: _ClassVar[int]
    PTY_INFO_FIELD_NUMBER: _ClassVar[int]
    container_id: str
    command: str
    args: _containers.RepeatedScalarFieldContainer[str]
    workdir: str
    env: _containers.ScalarMap[str, str]
    timeout_secs: int
    pty_info: PtyInfo
    def __init__(
        self,
        container_id: _Optional[str] = ...,
        command: _Optional[str] = ...,
        args: _Optional[_Iterable[str]] = ...,
        workdir: _Optional[str] = ...,
        env: _Optional[_Mapping[str, str]] = ...,
        timeout_secs: _Optional[int] = ...,
        pty_info: _Optional[_Union[PtyInfo, _Mapping]] = ...,
    ) -> None: ...

class PtyInfo(_message.Message):
    __slots__ = ("cols", "rows")
    COLS_FIELD_NUMBER: _ClassVar[int]
    ROWS_FIELD_NUMBER: _ClassVar[int]
    cols: int
    rows: int
    def __init__(
        self, cols: _Optional[int] = ..., rows: _Optional[int] = ...
    ) -> None: ...

class AttachSession(_message.Message):
    __slots__ = ("container_id", "session_id", "pty_info")
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    PTY_INFO_FIELD_NUMBER: _ClassVar[int]
    container_id: str
    session_id: str
    pty_info: PtyInfo
    def __init__(
        self,
        container_id: _Optional[str] = ...,
        session_id: _Optional[str] = ...,
        pty_info: _Optional[_Union[PtyInfo, _Mapping]] = ...,
    ) -> None: ...

class SessionAttached(_message.Message):
    __slots__ = ("session_id",)
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    def __init__(self, session_id: _Optional[str] = ...) -> None: ...

class DetachSession(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class SessionDetached(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class StdinData(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...

class StdinEof(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class SessionSignal(_message.Message):
    __slots__ = ("signal",)
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    signal: int
    def __init__(self, signal: _Optional[int] = ...) -> None: ...

class OutputData(_message.Message):
    __slots__ = ("stream", "data")
    class Stream(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        STREAM_UNSPECIFIED: _ClassVar[OutputData.Stream]
        STREAM_STDOUT: _ClassVar[OutputData.Stream]
        STREAM_STDERR: _ClassVar[OutputData.Stream]
        STREAM_PTY_OUTPUT: _ClassVar[OutputData.Stream]

    STREAM_UNSPECIFIED: OutputData.Stream
    STREAM_STDOUT: OutputData.Stream
    STREAM_STDERR: OutputData.Stream
    STREAM_PTY_OUTPUT: OutputData.Stream
    STREAM_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    stream: OutputData.Stream
    data: bytes
    def __init__(
        self,
        stream: _Optional[_Union[OutputData.Stream, str]] = ...,
        data: _Optional[bytes] = ...,
    ) -> None: ...

class GetProcessStatus(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ProcessStatus(_message.Message):
    __slots__ = ("state", "exit_code", "signal")
    STATE_FIELD_NUMBER: _ClassVar[int]
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    state: ProcessState
    exit_code: int
    signal: int
    def __init__(
        self,
        state: _Optional[_Union[ProcessState, str]] = ...,
        exit_code: _Optional[int] = ...,
        signal: _Optional[int] = ...,
    ) -> None: ...

class ProcessExited(_message.Message):
    __slots__ = ("exit_code", "signal")
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    exit_code: int
    signal: int
    def __init__(
        self, exit_code: _Optional[int] = ..., signal: _Optional[int] = ...
    ) -> None: ...

class ProcessFailed(_message.Message):
    __slots__ = ("message",)
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    message: str
    def __init__(self, message: _Optional[str] = ...) -> None: ...

class ProcessTimedOut(_message.Message):
    __slots__ = ("exit_code", "signal")
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    exit_code: int
    signal: int
    def __init__(
        self, exit_code: _Optional[int] = ..., signal: _Optional[int] = ...
    ) -> None: ...

class SessionInfo(_message.Message):
    __slots__ = ("session_id", "new_process", "process_status")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    NEW_PROCESS_FIELD_NUMBER: _ClassVar[int]
    PROCESS_STATUS_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    new_process: NewProcess
    process_status: ProcessStatus
    def __init__(
        self,
        session_id: _Optional[str] = ...,
        new_process: _Optional[_Union[NewProcess, _Mapping]] = ...,
        process_status: _Optional[_Union[ProcessStatus, _Mapping]] = ...,
    ) -> None: ...

class GetSessionRequest(_message.Message):
    __slots__ = ("container_id", "session_id")
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    container_id: str
    session_id: str
    def __init__(
        self, container_id: _Optional[str] = ..., session_id: _Optional[str] = ...
    ) -> None: ...

class GetSessionResponse(_message.Message):
    __slots__ = ("session",)
    SESSION_FIELD_NUMBER: _ClassVar[int]
    session: SessionInfo
    def __init__(
        self, session: _Optional[_Union[SessionInfo, _Mapping]] = ...
    ) -> None: ...

class ListSessionsRequest(_message.Message):
    __slots__ = ("container_id",)
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    container_id: str
    def __init__(self, container_id: _Optional[str] = ...) -> None: ...

class ListSessionsResponse(_message.Message):
    __slots__ = ("sessions",)
    SESSIONS_FIELD_NUMBER: _ClassVar[int]
    sessions: _containers.RepeatedCompositeFieldContainer[SessionInfo]
    def __init__(
        self, sessions: _Optional[_Iterable[_Union[SessionInfo, _Mapping]]] = ...
    ) -> None: ...

class ContainerHostInfo(_message.Message):
    __slots__ = ("host_id",)
    HOST_ID_FIELD_NUMBER: _ClassVar[int]
    host_id: str
    def __init__(self, host_id: _Optional[str] = ...) -> None: ...

class UpdateContainerStatusRequest(_message.Message):
    __slots__ = ("container_id", "status", "status_message", "host_info")
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    HOST_INFO_FIELD_NUMBER: _ClassVar[int]
    container_id: str
    status: str
    status_message: str
    host_info: ContainerHostInfo
    def __init__(
        self,
        container_id: _Optional[str] = ...,
        status: _Optional[str] = ...,
        status_message: _Optional[str] = ...,
        host_info: _Optional[_Union[ContainerHostInfo, _Mapping]] = ...,
    ) -> None: ...

class UpdateContainerStatusResponse(_message.Message):
    __slots__ = ("container",)
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    container: ContainerResponse
    def __init__(
        self, container: _Optional[_Union[ContainerResponse, _Mapping]] = ...
    ) -> None: ...

class BatchUpdateContainerStatusRequest(_message.Message):
    __slots__ = ("updates",)
    UPDATES_FIELD_NUMBER: _ClassVar[int]
    updates: _containers.RepeatedCompositeFieldContainer[UpdateContainerStatusRequest]
    def __init__(
        self,
        updates: _Optional[
            _Iterable[_Union[UpdateContainerStatusRequest, _Mapping]]
        ] = ...,
    ) -> None: ...

class BatchUpdateContainerStatusResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GKEPodSnapshot(_message.Message):
    __slots__ = ("storage_bucket", "storage_path")
    STORAGE_BUCKET_FIELD_NUMBER: _ClassVar[int]
    STORAGE_PATH_FIELD_NUMBER: _ClassVar[int]
    storage_bucket: str
    storage_path: str
    def __init__(
        self, storage_bucket: _Optional[str] = ..., storage_path: _Optional[str] = ...
    ) -> None: ...

class ContainerSnapshotSpec(_message.Message):
    __slots__ = ("gke_pod_snapshot",)
    GKE_POD_SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    gke_pod_snapshot: GKEPodSnapshot
    def __init__(
        self, gke_pod_snapshot: _Optional[_Union[GKEPodSnapshot, _Mapping]] = ...
    ) -> None: ...

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
    def __init__(
        self, id: _Optional[str] = ..., name: _Optional[str] = ...
    ) -> None: ...

class SnapshotContainerResponse(_message.Message):
    __slots__ = ("snapshot",)
    SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    snapshot: ContainerSnapshot
    def __init__(
        self, snapshot: _Optional[_Union[ContainerSnapshot, _Mapping]] = ...
    ) -> None: ...

class GetContainerSnapshotRequest(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class GetContainerSnapshotResponse(_message.Message):
    __slots__ = ("snapshot",)
    SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    snapshot: ContainerSnapshot
    def __init__(
        self, snapshot: _Optional[_Union[ContainerSnapshot, _Mapping]] = ...
    ) -> None: ...

class ListContainerSnapshotsRequest(_message.Message):
    __slots__ = ("source_container_id", "cursor", "limit")
    SOURCE_CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    source_container_id: str
    cursor: str
    limit: int
    def __init__(
        self,
        source_container_id: _Optional[str] = ...,
        cursor: _Optional[str] = ...,
        limit: _Optional[int] = ...,
    ) -> None: ...

class ListContainerSnapshotsResponse(_message.Message):
    __slots__ = ("snapshots", "next_cursor")
    SNAPSHOTS_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    snapshots: _containers.RepeatedCompositeFieldContainer[ContainerSnapshot]
    next_cursor: str
    def __init__(
        self,
        snapshots: _Optional[_Iterable[_Union[ContainerSnapshot, _Mapping]]] = ...,
        next_cursor: _Optional[str] = ...,
    ) -> None: ...

class ContainerTTYInput(_message.Message):
    __slots__ = ("data", "resize")
    DATA_FIELD_NUMBER: _ClassVar[int]
    RESIZE_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    resize: ContainerTerminalSize
    def __init__(
        self,
        data: _Optional[bytes] = ...,
        resize: _Optional[_Union[ContainerTerminalSize, _Mapping]] = ...,
    ) -> None: ...

class ContainerTerminalSize(_message.Message):
    __slots__ = ("rows", "cols")
    ROWS_FIELD_NUMBER: _ClassVar[int]
    COLS_FIELD_NUMBER: _ClassVar[int]
    rows: int
    cols: int
    def __init__(
        self, rows: _Optional[int] = ..., cols: _Optional[int] = ...
    ) -> None: ...

class CreateContainerDebugTTYRequest(_message.Message):
    __slots__ = ("init_request", "input")
    INIT_REQUEST_FIELD_NUMBER: _ClassVar[int]
    INPUT_FIELD_NUMBER: _ClassVar[int]
    init_request: ContainerDebugTTYInitRequest
    input: ContainerTTYInput
    def __init__(
        self,
        init_request: _Optional[_Union[ContainerDebugTTYInitRequest, _Mapping]] = ...,
        input: _Optional[_Union[ContainerTTYInput, _Mapping]] = ...,
    ) -> None: ...

class ContainerDebugTTYInitRequest(_message.Message):
    __slots__ = ("id", "name", "command")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    command: _containers.RepeatedScalarFieldContainer[str]
    def __init__(
        self,
        id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        command: _Optional[_Iterable[str]] = ...,
    ) -> None: ...

class CreateContainerDebugTTYResponse(_message.Message):
    __slots__ = ("data", "error", "closed")
    DATA_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    CLOSED_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    error: str
    closed: bool
    def __init__(
        self,
        data: _Optional[bytes] = ...,
        error: _Optional[str] = ...,
        closed: bool = ...,
    ) -> None: ...
