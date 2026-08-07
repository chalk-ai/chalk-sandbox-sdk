from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
from google.protobuf import duration_pb2 as _duration_pb2
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

class ProcessState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PROCESS_STATE_UNSPECIFIED: _ClassVar[ProcessState]
    PROCESS_STATE_RUNNING: _ClassVar[ProcessState]
    PROCESS_STATE_EXITED: _ClassVar[ProcessState]
    PROCESS_STATE_FAILED: _ClassVar[ProcessState]
    PROCESS_STATE_TIMED_OUT: _ClassVar[ProcessState]

PROCESS_STATE_UNSPECIFIED: ProcessState
PROCESS_STATE_RUNNING: ProcessState
PROCESS_STATE_EXITED: ProcessState
PROCESS_STATE_FAILED: ProcessState
PROCESS_STATE_TIMED_OUT: ProcessState

class ExecCommandRequest(_message.Message):
    __slots__ = (
        "sandbox_id",
        "command",
        "args",
        "workdir",
        "env_vars",
        "timeout",
        "stdin",
    )
    class EnvVarsEntry(_message.Message):
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
    ENV_VARS_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    STDIN_FIELD_NUMBER: _ClassVar[int]
    sandbox_id: str
    command: str
    args: _containers.RepeatedScalarFieldContainer[str]
    workdir: str
    env_vars: _containers.ScalarMap[str, str]
    timeout: _duration_pb2.Duration
    stdin: bytes
    def __init__(
        self,
        sandbox_id: _Optional[str] = ...,
        command: _Optional[str] = ...,
        args: _Optional[_Iterable[str]] = ...,
        workdir: _Optional[str] = ...,
        env_vars: _Optional[_Mapping[str, str]] = ...,
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

class PtyInfo(_message.Message):
    __slots__ = ("cols", "rows")
    COLS_FIELD_NUMBER: _ClassVar[int]
    ROWS_FIELD_NUMBER: _ClassVar[int]
    cols: int
    rows: int
    def __init__(
        self, cols: _Optional[int] = ..., rows: _Optional[int] = ...
    ) -> None: ...

class ResizePty(_message.Message):
    __slots__ = ("cols", "rows")
    COLS_FIELD_NUMBER: _ClassVar[int]
    ROWS_FIELD_NUMBER: _ClassVar[int]
    cols: int
    rows: int
    def __init__(
        self, cols: _Optional[int] = ..., rows: _Optional[int] = ...
    ) -> None: ...

class NewProcess(_message.Message):
    __slots__ = (
        "sandbox_id",
        "command",
        "args",
        "workdir",
        "env_vars",
        "timeout",
        "pty_info",
    )
    class EnvVarsEntry(_message.Message):
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
    ENV_VARS_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    PTY_INFO_FIELD_NUMBER: _ClassVar[int]
    sandbox_id: str
    command: str
    args: _containers.RepeatedScalarFieldContainer[str]
    workdir: str
    env_vars: _containers.ScalarMap[str, str]
    timeout: _duration_pb2.Duration
    pty_info: PtyInfo
    def __init__(
        self,
        sandbox_id: _Optional[str] = ...,
        command: _Optional[str] = ...,
        args: _Optional[_Iterable[str]] = ...,
        workdir: _Optional[str] = ...,
        env_vars: _Optional[_Mapping[str, str]] = ...,
        timeout: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ...,
        pty_info: _Optional[_Union[PtyInfo, _Mapping]] = ...,
    ) -> None: ...

class AttachSession(_message.Message):
    __slots__ = ("sandbox_id", "session_id", "pty_info")
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    PTY_INFO_FIELD_NUMBER: _ClassVar[int]
    sandbox_id: str
    session_id: str
    pty_info: PtyInfo
    def __init__(
        self,
        sandbox_id: _Optional[str] = ...,
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

class GetProcessStatus(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

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

class SessionError(_message.Message):
    __slots__ = ("code", "message")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    code: str
    message: str
    def __init__(
        self, code: _Optional[str] = ..., message: _Optional[str] = ...
    ) -> None: ...

class OpenSessionRequest(_message.Message):
    __slots__ = (
        "new_process",
        "attach_session",
        "detach_session",
        "stdin_data",
        "stdin_eof",
        "signal",
        "resize_pty",
        "get_process_status",
    )
    NEW_PROCESS_FIELD_NUMBER: _ClassVar[int]
    ATTACH_SESSION_FIELD_NUMBER: _ClassVar[int]
    DETACH_SESSION_FIELD_NUMBER: _ClassVar[int]
    STDIN_DATA_FIELD_NUMBER: _ClassVar[int]
    STDIN_EOF_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    RESIZE_PTY_FIELD_NUMBER: _ClassVar[int]
    GET_PROCESS_STATUS_FIELD_NUMBER: _ClassVar[int]
    new_process: NewProcess
    attach_session: AttachSession
    detach_session: DetachSession
    stdin_data: StdinData
    stdin_eof: StdinEof
    signal: SessionSignal
    resize_pty: ResizePty
    get_process_status: GetProcessStatus
    def __init__(
        self,
        new_process: _Optional[_Union[NewProcess, _Mapping]] = ...,
        attach_session: _Optional[_Union[AttachSession, _Mapping]] = ...,
        detach_session: _Optional[_Union[DetachSession, _Mapping]] = ...,
        stdin_data: _Optional[_Union[StdinData, _Mapping]] = ...,
        stdin_eof: _Optional[_Union[StdinEof, _Mapping]] = ...,
        signal: _Optional[_Union[SessionSignal, _Mapping]] = ...,
        resize_pty: _Optional[_Union[ResizePty, _Mapping]] = ...,
        get_process_status: _Optional[_Union[GetProcessStatus, _Mapping]] = ...,
    ) -> None: ...

class OpenSessionResponse(_message.Message):
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
    __slots__ = ("sandbox_id", "session_id")
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    sandbox_id: str
    session_id: str
    def __init__(
        self, sandbox_id: _Optional[str] = ..., session_id: _Optional[str] = ...
    ) -> None: ...

class GetSessionResponse(_message.Message):
    __slots__ = ("session",)
    SESSION_FIELD_NUMBER: _ClassVar[int]
    session: SessionInfo
    def __init__(
        self, session: _Optional[_Union[SessionInfo, _Mapping]] = ...
    ) -> None: ...

class ListSessionsRequest(_message.Message):
    __slots__ = ("sandbox_id",)
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    sandbox_id: str
    def __init__(self, sandbox_id: _Optional[str] = ...) -> None: ...

class ListSessionsResponse(_message.Message):
    __slots__ = ("sessions",)
    SESSIONS_FIELD_NUMBER: _ClassVar[int]
    sessions: _containers.RepeatedCompositeFieldContainer[SessionInfo]
    def __init__(
        self, sessions: _Optional[_Iterable[_Union[SessionInfo, _Mapping]]] = ...
    ) -> None: ...
