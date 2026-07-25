from chalkcompute._gen.chalk.artifactstore.v1 import service_pb2 as _service_pb2
from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
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

class PythonExecutionOutcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PYTHON_EXECUTION_OUTCOME_UNSPECIFIED: _ClassVar[PythonExecutionOutcome]
    PYTHON_EXECUTION_OUTCOME_SUCCEEDED: _ClassVar[PythonExecutionOutcome]
    PYTHON_EXECUTION_OUTCOME_FAILED: _ClassVar[PythonExecutionOutcome]
    PYTHON_EXECUTION_OUTCOME_TIMED_OUT: _ClassVar[PythonExecutionOutcome]
    PYTHON_EXECUTION_OUTCOME_INTERRUPTED: _ClassVar[PythonExecutionOutcome]

PYTHON_EXECUTION_OUTCOME_UNSPECIFIED: PythonExecutionOutcome
PYTHON_EXECUTION_OUTCOME_SUCCEEDED: PythonExecutionOutcome
PYTHON_EXECUTION_OUTCOME_FAILED: PythonExecutionOutcome
PYTHON_EXECUTION_OUTCOME_TIMED_OUT: PythonExecutionOutcome
PYTHON_EXECUTION_OUTCOME_INTERRUPTED: PythonExecutionOutcome

class ExecutePythonRequest(_message.Message):
    __slots__ = ("spec", "interrupt")
    SPEC_FIELD_NUMBER: _ClassVar[int]
    INTERRUPT_FIELD_NUMBER: _ClassVar[int]
    spec: ExecutePythonSpec
    interrupt: ExecutePythonInterrupt
    def __init__(
        self,
        spec: _Optional[_Union[ExecutePythonSpec, _Mapping]] = ...,
        interrupt: _Optional[_Union[ExecutePythonInterrupt, _Mapping]] = ...,
    ) -> None: ...

class ExecutePythonSpec(_message.Message):
    __slots__ = (
        "sandbox_id",
        "source",
        "timeout_secs",
        "env",
        "workdir",
        "capture_charts",
        "max_artifact_bytes",
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
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECS_FIELD_NUMBER: _ClassVar[int]
    ENV_FIELD_NUMBER: _ClassVar[int]
    WORKDIR_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_CHARTS_FIELD_NUMBER: _ClassVar[int]
    MAX_ARTIFACT_BYTES_FIELD_NUMBER: _ClassVar[int]
    sandbox_id: str
    source: str
    timeout_secs: int
    env: _containers.ScalarMap[str, str]
    workdir: str
    capture_charts: bool
    max_artifact_bytes: int
    def __init__(
        self,
        sandbox_id: _Optional[str] = ...,
        source: _Optional[str] = ...,
        timeout_secs: _Optional[int] = ...,
        env: _Optional[_Mapping[str, str]] = ...,
        workdir: _Optional[str] = ...,
        capture_charts: bool = ...,
        max_artifact_bytes: _Optional[int] = ...,
    ) -> None: ...

class ExecutePythonInterrupt(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ExecutePythonResponse(_message.Message):
    __slots__ = ("started", "output", "artifact", "completed")
    STARTED_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_FIELD_NUMBER: _ClassVar[int]
    started: PythonExecutionStarted
    output: PythonOutput
    artifact: _service_pb2.Artifact
    completed: PythonExecutionCompleted
    def __init__(
        self,
        started: _Optional[_Union[PythonExecutionStarted, _Mapping]] = ...,
        output: _Optional[_Union[PythonOutput, _Mapping]] = ...,
        artifact: _Optional[_Union[_service_pb2.Artifact, _Mapping]] = ...,
        completed: _Optional[_Union[PythonExecutionCompleted, _Mapping]] = ...,
    ) -> None: ...

class PythonExecutionStarted(_message.Message):
    __slots__ = ("execution_id",)
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    execution_id: str
    def __init__(self, execution_id: _Optional[str] = ...) -> None: ...

class PythonOutput(_message.Message):
    __slots__ = ("stream", "data")
    class Stream(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        STREAM_UNSPECIFIED: _ClassVar[PythonOutput.Stream]
        STREAM_STDOUT: _ClassVar[PythonOutput.Stream]
        STREAM_STDERR: _ClassVar[PythonOutput.Stream]

    STREAM_UNSPECIFIED: PythonOutput.Stream
    STREAM_STDOUT: PythonOutput.Stream
    STREAM_STDERR: PythonOutput.Stream
    STREAM_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    stream: PythonOutput.Stream
    data: bytes
    def __init__(
        self,
        stream: _Optional[_Union[PythonOutput.Stream, str]] = ...,
        data: _Optional[bytes] = ...,
    ) -> None: ...

class SkippedArtifact(_message.Message):
    __slots__ = ("name", "reason", "byte_size")
    NAME_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    BYTE_SIZE_FIELD_NUMBER: _ClassVar[int]
    name: str
    reason: str
    byte_size: int
    def __init__(
        self,
        name: _Optional[str] = ...,
        reason: _Optional[str] = ...,
        byte_size: _Optional[int] = ...,
    ) -> None: ...

class PythonExecutionCompleted(_message.Message):
    __slots__ = ("exit_code", "outcome", "error_message", "skipped_artifacts")
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    SKIPPED_ARTIFACTS_FIELD_NUMBER: _ClassVar[int]
    exit_code: int
    outcome: PythonExecutionOutcome
    error_message: str
    skipped_artifacts: _containers.RepeatedCompositeFieldContainer[SkippedArtifact]
    def __init__(
        self,
        exit_code: _Optional[int] = ...,
        outcome: _Optional[_Union[PythonExecutionOutcome, str]] = ...,
        error_message: _Optional[str] = ...,
        skipped_artifacts: _Optional[
            _Iterable[_Union[SkippedArtifact, _Mapping]]
        ] = ...,
    ) -> None: ...
