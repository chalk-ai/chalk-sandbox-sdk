from chalkcompute._gen.chalk.argo.v1 import workflow_pb2 as _workflow_pb2
from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
from chalkcompute._gen.chalk.common.v1 import log_pb2 as _log_pb2
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

class ExecRequest(_message.Message):
    __slots__ = ("init", "stdin_data", "stdin_eof", "signal", "resize")
    INIT_FIELD_NUMBER: _ClassVar[int]
    STDIN_DATA_FIELD_NUMBER: _ClassVar[int]
    STDIN_EOF_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    RESIZE_FIELD_NUMBER: _ClassVar[int]
    init: ExecInit
    stdin_data: StdinData
    stdin_eof: StdinEof
    signal: ExecSignal
    resize: ExecResize
    def __init__(
        self,
        init: _Optional[_Union[ExecInit, _Mapping]] = ...,
        stdin_data: _Optional[_Union[StdinData, _Mapping]] = ...,
        stdin_eof: _Optional[_Union[StdinEof, _Mapping]] = ...,
        signal: _Optional[_Union[ExecSignal, _Mapping]] = ...,
        resize: _Optional[_Union[ExecResize, _Mapping]] = ...,
    ) -> None: ...

class ExecInit(_message.Message):
    __slots__ = (
        "sandbox_id",
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

    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    WORKDIR_FIELD_NUMBER: _ClassVar[int]
    ENV_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECS_FIELD_NUMBER: _ClassVar[int]
    PTY_INFO_FIELD_NUMBER: _ClassVar[int]
    sandbox_id: str
    command: str
    args: _containers.RepeatedScalarFieldContainer[str]
    workdir: str
    env: _containers.ScalarMap[str, str]
    timeout_secs: int
    pty_info: PtyInfo
    def __init__(
        self,
        sandbox_id: _Optional[str] = ...,
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

class StdinData(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...

class StdinEof(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ExecSignal(_message.Message):
    __slots__ = ("signal",)
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    signal: int
    def __init__(self, signal: _Optional[int] = ...) -> None: ...

class ExecResize(_message.Message):
    __slots__ = ("cols", "rows")
    COLS_FIELD_NUMBER: _ClassVar[int]
    ROWS_FIELD_NUMBER: _ClassVar[int]
    cols: int
    rows: int
    def __init__(
        self, cols: _Optional[int] = ..., rows: _Optional[int] = ...
    ) -> None: ...

class ExecResponse(_message.Message):
    __slots__ = ("process_started", "output_data", "process_exited", "error")
    PROCESS_STARTED_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_DATA_FIELD_NUMBER: _ClassVar[int]
    PROCESS_EXITED_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    process_started: ProcessStarted
    output_data: OutputData
    process_exited: ProcessExited
    error: ExecError
    def __init__(
        self,
        process_started: _Optional[_Union[ProcessStarted, _Mapping]] = ...,
        output_data: _Optional[_Union[OutputData, _Mapping]] = ...,
        process_exited: _Optional[_Union[ProcessExited, _Mapping]] = ...,
        error: _Optional[_Union[ExecError, _Mapping]] = ...,
    ) -> None: ...

class ProcessStarted(_message.Message):
    __slots__ = ("pid",)
    PID_FIELD_NUMBER: _ClassVar[int]
    pid: int
    def __init__(self, pid: _Optional[int] = ...) -> None: ...

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

class ProcessExited(_message.Message):
    __slots__ = ("exit_code", "signal")
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    exit_code: int
    signal: int
    def __init__(
        self, exit_code: _Optional[int] = ..., signal: _Optional[int] = ...
    ) -> None: ...

class ExecError(_message.Message):
    __slots__ = ("code", "message")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    code: str
    message: str
    def __init__(
        self, code: _Optional[str] = ..., message: _Optional[str] = ...
    ) -> None: ...

class ImageSpec(_message.Message):
    __slots__ = ("base_image", "steps", "entrypoint", "cmd", "workdir", "env")
    class EnvEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(
            self, key: _Optional[str] = ..., value: _Optional[str] = ...
        ) -> None: ...

    BASE_IMAGE_FIELD_NUMBER: _ClassVar[int]
    STEPS_FIELD_NUMBER: _ClassVar[int]
    ENTRYPOINT_FIELD_NUMBER: _ClassVar[int]
    CMD_FIELD_NUMBER: _ClassVar[int]
    WORKDIR_FIELD_NUMBER: _ClassVar[int]
    ENV_FIELD_NUMBER: _ClassVar[int]
    base_image: str
    steps: _containers.RepeatedCompositeFieldContainer[BuildStep]
    entrypoint: _containers.RepeatedScalarFieldContainer[str]
    cmd: _containers.RepeatedScalarFieldContainer[str]
    workdir: str
    env: _containers.ScalarMap[str, str]
    def __init__(
        self,
        base_image: _Optional[str] = ...,
        steps: _Optional[_Iterable[_Union[BuildStep, _Mapping]]] = ...,
        entrypoint: _Optional[_Iterable[str]] = ...,
        cmd: _Optional[_Iterable[str]] = ...,
        workdir: _Optional[str] = ...,
        env: _Optional[_Mapping[str, str]] = ...,
    ) -> None: ...

class BuildStep(_message.Message):
    __slots__ = (
        "run_commands",
        "pip_install",
        "add_file",
        "dockerfile_commands",
        "uv_pip_install",
        "uv_sync",
    )
    RUN_COMMANDS_FIELD_NUMBER: _ClassVar[int]
    PIP_INSTALL_FIELD_NUMBER: _ClassVar[int]
    ADD_FILE_FIELD_NUMBER: _ClassVar[int]
    DOCKERFILE_COMMANDS_FIELD_NUMBER: _ClassVar[int]
    UV_PIP_INSTALL_FIELD_NUMBER: _ClassVar[int]
    UV_SYNC_FIELD_NUMBER: _ClassVar[int]
    run_commands: RunCommandsStep
    pip_install: PipInstallStep
    add_file: AddFileStep
    dockerfile_commands: DockerfileCommandsStep
    uv_pip_install: UvPipInstallStep
    uv_sync: UvSyncStep
    def __init__(
        self,
        run_commands: _Optional[_Union[RunCommandsStep, _Mapping]] = ...,
        pip_install: _Optional[_Union[PipInstallStep, _Mapping]] = ...,
        add_file: _Optional[_Union[AddFileStep, _Mapping]] = ...,
        dockerfile_commands: _Optional[_Union[DockerfileCommandsStep, _Mapping]] = ...,
        uv_pip_install: _Optional[_Union[UvPipInstallStep, _Mapping]] = ...,
        uv_sync: _Optional[_Union[UvSyncStep, _Mapping]] = ...,
    ) -> None: ...

class RunCommandsStep(_message.Message):
    __slots__ = ("commands",)
    COMMANDS_FIELD_NUMBER: _ClassVar[int]
    commands: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, commands: _Optional[_Iterable[str]] = ...) -> None: ...

class PipInstallStep(_message.Message):
    __slots__ = ("packages",)
    PACKAGES_FIELD_NUMBER: _ClassVar[int]
    packages: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, packages: _Optional[_Iterable[str]] = ...) -> None: ...

class UvPipInstallStep(_message.Message):
    __slots__ = ("packages",)
    PACKAGES_FIELD_NUMBER: _ClassVar[int]
    packages: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, packages: _Optional[_Iterable[str]] = ...) -> None: ...

class UvSyncStep(_message.Message):
    __slots__ = ("pyproject_toml_lz4", "uv_lock_lz4", "extras", "workdir")
    PYPROJECT_TOML_LZ4_FIELD_NUMBER: _ClassVar[int]
    UV_LOCK_LZ4_FIELD_NUMBER: _ClassVar[int]
    EXTRAS_FIELD_NUMBER: _ClassVar[int]
    WORKDIR_FIELD_NUMBER: _ClassVar[int]
    pyproject_toml_lz4: bytes
    uv_lock_lz4: bytes
    extras: _containers.RepeatedScalarFieldContainer[str]
    workdir: str
    def __init__(
        self,
        pyproject_toml_lz4: _Optional[bytes] = ...,
        uv_lock_lz4: _Optional[bytes] = ...,
        extras: _Optional[_Iterable[str]] = ...,
        workdir: _Optional[str] = ...,
    ) -> None: ...

class AddFileStep(_message.Message):
    __slots__ = ("destination", "content", "mode")
    DESTINATION_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    destination: str
    content: bytes
    mode: int
    def __init__(
        self,
        destination: _Optional[str] = ...,
        content: _Optional[bytes] = ...,
        mode: _Optional[int] = ...,
    ) -> None: ...

class DockerfileCommandsStep(_message.Message):
    __slots__ = ("commands",)
    COMMANDS_FIELD_NUMBER: _ClassVar[int]
    commands: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, commands: _Optional[_Iterable[str]] = ...) -> None: ...

class BuildCustomImageRequest(_message.Message):
    __slots__ = ("image_spec", "target_registry", "tag")
    IMAGE_SPEC_FIELD_NUMBER: _ClassVar[int]
    TARGET_REGISTRY_FIELD_NUMBER: _ClassVar[int]
    TAG_FIELD_NUMBER: _ClassVar[int]
    image_spec: ImageSpec
    target_registry: str
    tag: str
    def __init__(
        self,
        image_spec: _Optional[_Union[ImageSpec, _Mapping]] = ...,
        target_registry: _Optional[str] = ...,
        tag: _Optional[str] = ...,
    ) -> None: ...

class BuildCustomImageResponse(_message.Message):
    __slots__ = ("image", "build_id")
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    BUILD_ID_FIELD_NUMBER: _ClassVar[int]
    image: str
    build_id: str
    def __init__(
        self, image: _Optional[str] = ..., build_id: _Optional[str] = ...
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

class CreateSandboxRequest(_message.Message):
    __slots__ = (
        "image",
        "image_spec",
        "resource_limits",
        "env",
        "name",
        "volumes",
        "runtime",
        "entrypoint",
        "knowledge_cutoff",
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

    IMAGE_FIELD_NUMBER: _ClassVar[int]
    IMAGE_SPEC_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_LIMITS_FIELD_NUMBER: _ClassVar[int]
    ENV_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    VOLUMES_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_FIELD_NUMBER: _ClassVar[int]
    ENTRYPOINT_FIELD_NUMBER: _ClassVar[int]
    KNOWLEDGE_CUTOFF_FIELD_NUMBER: _ClassVar[int]
    image: str
    image_spec: ImageSpec
    resource_limits: ResourceLimits
    env: _containers.ScalarMap[str, str]
    name: str
    volumes: _containers.RepeatedCompositeFieldContainer[VolumeMount]
    runtime: str
    entrypoint: _containers.RepeatedScalarFieldContainer[str]
    knowledge_cutoff: _timestamp_pb2.Timestamp
    def __init__(
        self,
        image: _Optional[str] = ...,
        image_spec: _Optional[_Union[ImageSpec, _Mapping]] = ...,
        resource_limits: _Optional[_Union[ResourceLimits, _Mapping]] = ...,
        env: _Optional[_Mapping[str, str]] = ...,
        name: _Optional[str] = ...,
        volumes: _Optional[_Iterable[_Union[VolumeMount, _Mapping]]] = ...,
        runtime: _Optional[str] = ...,
        entrypoint: _Optional[_Iterable[str]] = ...,
        knowledge_cutoff: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
    ) -> None: ...

class ResourceLimits(_message.Message):
    __slots__ = ("cpu", "memory")
    CPU_FIELD_NUMBER: _ClassVar[int]
    MEMORY_FIELD_NUMBER: _ClassVar[int]
    cpu: str
    memory: str
    def __init__(
        self, cpu: _Optional[str] = ..., memory: _Optional[str] = ...
    ) -> None: ...

class CreateSandboxResponse(_message.Message):
    __slots__ = ("sandbox",)
    SANDBOX_FIELD_NUMBER: _ClassVar[int]
    sandbox: SandboxInfo
    def __init__(
        self, sandbox: _Optional[_Union[SandboxInfo, _Mapping]] = ...
    ) -> None: ...

class TerminateSandboxRequest(_message.Message):
    __slots__ = ("sandbox_id", "grace_period_seconds")
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    GRACE_PERIOD_SECONDS_FIELD_NUMBER: _ClassVar[int]
    sandbox_id: str
    grace_period_seconds: int
    def __init__(
        self,
        sandbox_id: _Optional[str] = ...,
        grace_period_seconds: _Optional[int] = ...,
    ) -> None: ...

class TerminateSandboxResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetSandboxRequest(_message.Message):
    __slots__ = ("sandbox_id",)
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    sandbox_id: str
    def __init__(self, sandbox_id: _Optional[str] = ...) -> None: ...

class GetSandboxResponse(_message.Message):
    __slots__ = ("sandbox",)
    SANDBOX_FIELD_NUMBER: _ClassVar[int]
    sandbox: SandboxInfo
    def __init__(
        self, sandbox: _Optional[_Union[SandboxInfo, _Mapping]] = ...
    ) -> None: ...

class ListSandboxesRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListSandboxesResponse(_message.Message):
    __slots__ = ("sandboxes",)
    SANDBOXES_FIELD_NUMBER: _ClassVar[int]
    sandboxes: _containers.RepeatedCompositeFieldContainer[SandboxInfo]
    def __init__(
        self, sandboxes: _Optional[_Iterable[_Union[SandboxInfo, _Mapping]]] = ...
    ) -> None: ...

class SandboxInfo(_message.Message):
    __slots__ = ("id", "state", "created_at", "name", "build_id", "knowledge_cutoff")
    ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    BUILD_ID_FIELD_NUMBER: _ClassVar[int]
    KNOWLEDGE_CUTOFF_FIELD_NUMBER: _ClassVar[int]
    id: str
    state: str
    created_at: str
    name: str
    build_id: str
    knowledge_cutoff: _timestamp_pb2.Timestamp
    def __init__(
        self,
        id: _Optional[str] = ...,
        state: _Optional[str] = ...,
        created_at: _Optional[str] = ...,
        name: _Optional[str] = ...,
        build_id: _Optional[str] = ...,
        knowledge_cutoff: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
    ) -> None: ...

class GetCustomImageRequest(_message.Message):
    __slots__ = ("build_id",)
    BUILD_ID_FIELD_NUMBER: _ClassVar[int]
    build_id: str
    def __init__(self, build_id: _Optional[str] = ...) -> None: ...

class GetCustomImageResponse(_message.Message):
    __slots__ = ("build_id", "status", "image", "error")
    BUILD_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    build_id: str
    status: str
    image: str
    error: str
    def __init__(
        self,
        build_id: _Optional[str] = ...,
        status: _Optional[str] = ...,
        image: _Optional[str] = ...,
        error: _Optional[str] = ...,
    ) -> None: ...

class GetOrBuildCustomImageRequest(_message.Message):
    __slots__ = ("image_spec", "target_registry")
    IMAGE_SPEC_FIELD_NUMBER: _ClassVar[int]
    TARGET_REGISTRY_FIELD_NUMBER: _ClassVar[int]
    image_spec: ImageSpec
    target_registry: str
    def __init__(
        self,
        image_spec: _Optional[_Union[ImageSpec, _Mapping]] = ...,
        target_registry: _Optional[str] = ...,
    ) -> None: ...

class GetOrBuildCustomImageResponse(_message.Message):
    __slots__ = ("image", "build_id", "status", "error")
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    BUILD_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    image: str
    build_id: str
    status: str
    error: str
    def __init__(
        self,
        image: _Optional[str] = ...,
        build_id: _Optional[str] = ...,
        status: _Optional[str] = ...,
        error: _Optional[str] = ...,
    ) -> None: ...

class StreamCustomImageBuildUpdatesRequest(_message.Message):
    __slots__ = ("build_id",)
    BUILD_ID_FIELD_NUMBER: _ClassVar[int]
    build_id: str
    def __init__(self, build_id: _Optional[str] = ...) -> None: ...

class StreamCustomImageBuildUpdatesResponse(_message.Message):
    __slots__ = ("status", "image", "error", "message")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    status: str
    image: str
    error: str
    message: str
    def __init__(
        self,
        status: _Optional[str] = ...,
        image: _Optional[str] = ...,
        error: _Optional[str] = ...,
        message: _Optional[str] = ...,
    ) -> None: ...

class CustomImageBuildSummary(_message.Message):
    __slots__ = ("content_hash", "image_ref", "created_at", "build_id", "status")
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    IMAGE_REF_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    BUILD_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    content_hash: str
    image_ref: str
    created_at: _timestamp_pb2.Timestamp
    build_id: str
    status: str
    def __init__(
        self,
        content_hash: _Optional[str] = ...,
        image_ref: _Optional[str] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        build_id: _Optional[str] = ...,
        status: _Optional[str] = ...,
    ) -> None: ...

class ListCustomImageBuildsRequest(_message.Message):
    __slots__ = ("limit", "page_token")
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    limit: int
    page_token: str
    def __init__(
        self, limit: _Optional[int] = ..., page_token: _Optional[str] = ...
    ) -> None: ...

class ListCustomImageBuildsResponse(_message.Message):
    __slots__ = ("builds", "next_page_token")
    BUILDS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    builds: _containers.RepeatedCompositeFieldContainer[CustomImageBuildSummary]
    next_page_token: str
    def __init__(
        self,
        builds: _Optional[_Iterable[_Union[CustomImageBuildSummary, _Mapping]]] = ...,
        next_page_token: _Optional[str] = ...,
    ) -> None: ...

class GetCustomImageBuildRequest(_message.Message):
    __slots__ = ("content_hash", "build_id")
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    BUILD_ID_FIELD_NUMBER: _ClassVar[int]
    content_hash: str
    build_id: str
    def __init__(
        self, content_hash: _Optional[str] = ..., build_id: _Optional[str] = ...
    ) -> None: ...

class GetCustomImageBuildResponse(_message.Message):
    __slots__ = ("build", "image_spec")
    BUILD_FIELD_NUMBER: _ClassVar[int]
    IMAGE_SPEC_FIELD_NUMBER: _ClassVar[int]
    build: CustomImageBuildSummary
    image_spec: ImageSpec
    def __init__(
        self,
        build: _Optional[_Union[CustomImageBuildSummary, _Mapping]] = ...,
        image_spec: _Optional[_Union[ImageSpec, _Mapping]] = ...,
    ) -> None: ...

class GetCustomImageBuildLogsRequest(_message.Message):
    __slots__ = (
        "content_hash",
        "build_id",
        "start_time",
        "end_time",
        "limit",
        "page_token",
    )
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    BUILD_ID_FIELD_NUMBER: _ClassVar[int]
    START_TIME_FIELD_NUMBER: _ClassVar[int]
    END_TIME_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    content_hash: str
    build_id: str
    start_time: _timestamp_pb2.Timestamp
    end_time: _timestamp_pb2.Timestamp
    limit: int
    page_token: str
    def __init__(
        self,
        content_hash: _Optional[str] = ...,
        build_id: _Optional[str] = ...,
        start_time: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        end_time: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        limit: _Optional[int] = ...,
        page_token: _Optional[str] = ...,
    ) -> None: ...

class GetCustomImageBuildLogsResponse(_message.Message):
    __slots__ = ("logs", "next_page_token")
    LOGS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    logs: _containers.RepeatedCompositeFieldContainer[_log_pb2.LogEntry]
    next_page_token: str
    def __init__(
        self,
        logs: _Optional[_Iterable[_Union[_log_pb2.LogEntry, _Mapping]]] = ...,
        next_page_token: _Optional[str] = ...,
    ) -> None: ...

class GetCustomImageBuildWorkflowRequest(_message.Message):
    __slots__ = ("content_hash", "build_id")
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    BUILD_ID_FIELD_NUMBER: _ClassVar[int]
    content_hash: str
    build_id: str
    def __init__(
        self, content_hash: _Optional[str] = ..., build_id: _Optional[str] = ...
    ) -> None: ...

class GetCustomImageBuildWorkflowResponse(_message.Message):
    __slots__ = ("workflow",)
    WORKFLOW_FIELD_NUMBER: _ClassVar[int]
    workflow: _workflow_pb2.ArgoWorkflow
    def __init__(
        self, workflow: _Optional[_Union[_workflow_pb2.ArgoWorkflow, _Mapping]] = ...
    ) -> None: ...

class GetCustomImageBuildUsageRequest(_message.Message):
    __slots__ = ("content_hash", "build_id")
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    BUILD_ID_FIELD_NUMBER: _ClassVar[int]
    content_hash: str
    build_id: str
    def __init__(
        self, content_hash: _Optional[str] = ..., build_id: _Optional[str] = ...
    ) -> None: ...

class CustomImageContainerUsage(_message.Message):
    __slots__ = ("id", "name", "status")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    status: str
    def __init__(
        self,
        id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        status: _Optional[str] = ...,
    ) -> None: ...

class CustomImageScalingGroupUsage(_message.Message):
    __slots__ = ("id", "name", "status")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    status: str
    def __init__(
        self,
        id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        status: _Optional[str] = ...,
    ) -> None: ...

class CustomImageSandboxUsage(_message.Message):
    __slots__ = ("id", "name", "status")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    status: str
    def __init__(
        self,
        id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        status: _Optional[str] = ...,
    ) -> None: ...

class GetCustomImageBuildUsageResponse(_message.Message):
    __slots__ = ("containers", "scaling_groups", "sandboxes")
    CONTAINERS_FIELD_NUMBER: _ClassVar[int]
    SCALING_GROUPS_FIELD_NUMBER: _ClassVar[int]
    SANDBOXES_FIELD_NUMBER: _ClassVar[int]
    containers: _containers.RepeatedCompositeFieldContainer[CustomImageContainerUsage]
    scaling_groups: _containers.RepeatedCompositeFieldContainer[
        CustomImageScalingGroupUsage
    ]
    sandboxes: _containers.RepeatedCompositeFieldContainer[CustomImageSandboxUsage]
    def __init__(
        self,
        containers: _Optional[
            _Iterable[_Union[CustomImageContainerUsage, _Mapping]]
        ] = ...,
        scaling_groups: _Optional[
            _Iterable[_Union[CustomImageScalingGroupUsage, _Mapping]]
        ] = ...,
        sandboxes: _Optional[
            _Iterable[_Union[CustomImageSandboxUsage, _Mapping]]
        ] = ...,
    ) -> None: ...
