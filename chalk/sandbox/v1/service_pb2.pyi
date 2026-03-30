from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ExecRequest(_message.Message):
    __slots__ = ("init", "stdin_data", "stdin_eof", "signal")
    INIT_FIELD_NUMBER: _ClassVar[int]
    STDIN_DATA_FIELD_NUMBER: _ClassVar[int]
    STDIN_EOF_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    init: ExecInit
    stdin_data: StdinData
    stdin_eof: StdinEof
    signal: ExecSignal
    def __init__(self, init: _Optional[_Union[ExecInit, _Mapping]] = ..., stdin_data: _Optional[_Union[StdinData, _Mapping]] = ..., stdin_eof: _Optional[_Union[StdinEof, _Mapping]] = ..., signal: _Optional[_Union[ExecSignal, _Mapping]] = ...) -> None: ...

class ExecInit(_message.Message):
    __slots__ = ("sandbox_id", "command", "args", "workdir", "env", "timeout_secs")
    class EnvEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    WORKDIR_FIELD_NUMBER: _ClassVar[int]
    ENV_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECS_FIELD_NUMBER: _ClassVar[int]
    sandbox_id: str
    command: str
    args: _containers.RepeatedScalarFieldContainer[str]
    workdir: str
    env: _containers.ScalarMap[str, str]
    timeout_secs: int
    def __init__(self, sandbox_id: _Optional[str] = ..., command: _Optional[str] = ..., args: _Optional[_Iterable[str]] = ..., workdir: _Optional[str] = ..., env: _Optional[_Mapping[str, str]] = ..., timeout_secs: _Optional[int] = ...) -> None: ...

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
    def __init__(self, process_started: _Optional[_Union[ProcessStarted, _Mapping]] = ..., output_data: _Optional[_Union[OutputData, _Mapping]] = ..., process_exited: _Optional[_Union[ProcessExited, _Mapping]] = ..., error: _Optional[_Union[ExecError, _Mapping]] = ...) -> None: ...

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
    STREAM_UNSPECIFIED: OutputData.Stream
    STREAM_STDOUT: OutputData.Stream
    STREAM_STDERR: OutputData.Stream
    STREAM_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    stream: OutputData.Stream
    data: bytes
    def __init__(self, stream: _Optional[_Union[OutputData.Stream, str]] = ..., data: _Optional[bytes] = ...) -> None: ...

class ProcessExited(_message.Message):
    __slots__ = ("exit_code", "signal")
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    exit_code: int
    signal: int
    def __init__(self, exit_code: _Optional[int] = ..., signal: _Optional[int] = ...) -> None: ...

class ExecError(_message.Message):
    __slots__ = ("code", "message")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    code: str
    message: str
    def __init__(self, code: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class ImageSpec(_message.Message):
    __slots__ = ("base_image", "steps", "entrypoint", "cmd", "workdir", "env")
    class EnvEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
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
    def __init__(self, base_image: _Optional[str] = ..., steps: _Optional[_Iterable[_Union[BuildStep, _Mapping]]] = ..., entrypoint: _Optional[_Iterable[str]] = ..., cmd: _Optional[_Iterable[str]] = ..., workdir: _Optional[str] = ..., env: _Optional[_Mapping[str, str]] = ...) -> None: ...

class BuildStep(_message.Message):
    __slots__ = ("run_commands", "pip_install", "add_file", "dockerfile_commands")
    RUN_COMMANDS_FIELD_NUMBER: _ClassVar[int]
    PIP_INSTALL_FIELD_NUMBER: _ClassVar[int]
    ADD_FILE_FIELD_NUMBER: _ClassVar[int]
    DOCKERFILE_COMMANDS_FIELD_NUMBER: _ClassVar[int]
    run_commands: RunCommandsStep
    pip_install: PipInstallStep
    add_file: AddFileStep
    dockerfile_commands: DockerfileCommandsStep
    def __init__(self, run_commands: _Optional[_Union[RunCommandsStep, _Mapping]] = ..., pip_install: _Optional[_Union[PipInstallStep, _Mapping]] = ..., add_file: _Optional[_Union[AddFileStep, _Mapping]] = ..., dockerfile_commands: _Optional[_Union[DockerfileCommandsStep, _Mapping]] = ...) -> None: ...

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

class AddFileStep(_message.Message):
    __slots__ = ("destination", "content", "mode")
    DESTINATION_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    destination: str
    content: bytes
    mode: int
    def __init__(self, destination: _Optional[str] = ..., content: _Optional[bytes] = ..., mode: _Optional[int] = ...) -> None: ...

class DockerfileCommandsStep(_message.Message):
    __slots__ = ("commands",)
    COMMANDS_FIELD_NUMBER: _ClassVar[int]
    commands: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, commands: _Optional[_Iterable[str]] = ...) -> None: ...

class CreateSandboxRequest(_message.Message):
    __slots__ = ("image", "image_spec", "resource_limits", "env", "name")
    class EnvEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    IMAGE_SPEC_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_LIMITS_FIELD_NUMBER: _ClassVar[int]
    ENV_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    image: str
    image_spec: ImageSpec
    resource_limits: ResourceLimits
    env: _containers.ScalarMap[str, str]
    name: str
    def __init__(self, image: _Optional[str] = ..., image_spec: _Optional[_Union[ImageSpec, _Mapping]] = ..., resource_limits: _Optional[_Union[ResourceLimits, _Mapping]] = ..., env: _Optional[_Mapping[str, str]] = ..., name: _Optional[str] = ...) -> None: ...

class ResourceLimits(_message.Message):
    __slots__ = ("cpu", "memory")
    CPU_FIELD_NUMBER: _ClassVar[int]
    MEMORY_FIELD_NUMBER: _ClassVar[int]
    cpu: str
    memory: str
    def __init__(self, cpu: _Optional[str] = ..., memory: _Optional[str] = ...) -> None: ...

class CreateSandboxResponse(_message.Message):
    __slots__ = ("sandbox",)
    SANDBOX_FIELD_NUMBER: _ClassVar[int]
    sandbox: SandboxInfo
    def __init__(self, sandbox: _Optional[_Union[SandboxInfo, _Mapping]] = ...) -> None: ...

class TerminateSandboxRequest(_message.Message):
    __slots__ = ("sandbox_id", "grace_period_seconds")
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    GRACE_PERIOD_SECONDS_FIELD_NUMBER: _ClassVar[int]
    sandbox_id: str
    grace_period_seconds: int
    def __init__(self, sandbox_id: _Optional[str] = ..., grace_period_seconds: _Optional[int] = ...) -> None: ...

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
    def __init__(self, sandbox: _Optional[_Union[SandboxInfo, _Mapping]] = ...) -> None: ...

class ListSandboxesRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListSandboxesResponse(_message.Message):
    __slots__ = ("sandboxes",)
    SANDBOXES_FIELD_NUMBER: _ClassVar[int]
    sandboxes: _containers.RepeatedCompositeFieldContainer[SandboxInfo]
    def __init__(self, sandboxes: _Optional[_Iterable[_Union[SandboxInfo, _Mapping]]] = ...) -> None: ...

class SandboxInfo(_message.Message):
    __slots__ = ("id", "state", "created_at", "name", "build_id")
    ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    BUILD_ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    state: str
    created_at: str
    name: str
    build_id: str
    def __init__(self, id: _Optional[str] = ..., state: _Optional[str] = ..., created_at: _Optional[str] = ..., name: _Optional[str] = ..., build_id: _Optional[str] = ...) -> None: ...
