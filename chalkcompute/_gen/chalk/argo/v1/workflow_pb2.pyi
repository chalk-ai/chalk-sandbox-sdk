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

class ArgoWorkflowPhase(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ARGO_WORKFLOW_PHASE_UNSPECIFIED: _ClassVar[ArgoWorkflowPhase]
    ARGO_WORKFLOW_PHASE_PENDING: _ClassVar[ArgoWorkflowPhase]
    ARGO_WORKFLOW_PHASE_RUNNING: _ClassVar[ArgoWorkflowPhase]
    ARGO_WORKFLOW_PHASE_SUCCEEDED: _ClassVar[ArgoWorkflowPhase]
    ARGO_WORKFLOW_PHASE_SKIPPED: _ClassVar[ArgoWorkflowPhase]
    ARGO_WORKFLOW_PHASE_FAILED: _ClassVar[ArgoWorkflowPhase]
    ARGO_WORKFLOW_PHASE_ERROR: _ClassVar[ArgoWorkflowPhase]
    ARGO_WORKFLOW_PHASE_OMITTED: _ClassVar[ArgoWorkflowPhase]

ARGO_WORKFLOW_PHASE_UNSPECIFIED: ArgoWorkflowPhase
ARGO_WORKFLOW_PHASE_PENDING: ArgoWorkflowPhase
ARGO_WORKFLOW_PHASE_RUNNING: ArgoWorkflowPhase
ARGO_WORKFLOW_PHASE_SUCCEEDED: ArgoWorkflowPhase
ARGO_WORKFLOW_PHASE_SKIPPED: ArgoWorkflowPhase
ARGO_WORKFLOW_PHASE_FAILED: ArgoWorkflowPhase
ARGO_WORKFLOW_PHASE_ERROR: ArgoWorkflowPhase
ARGO_WORKFLOW_PHASE_OMITTED: ArgoWorkflowPhase

class ArgoWorkflowMetadata(_message.Message):
    __slots__ = ("name", "namespace", "creation_timestamp", "labels", "annotations")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(
            self, key: _Optional[str] = ..., value: _Optional[str] = ...
        ) -> None: ...

    class AnnotationsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(
            self, key: _Optional[str] = ..., value: _Optional[str] = ...
        ) -> None: ...

    NAME_FIELD_NUMBER: _ClassVar[int]
    NAMESPACE_FIELD_NUMBER: _ClassVar[int]
    CREATION_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    ANNOTATIONS_FIELD_NUMBER: _ClassVar[int]
    name: str
    namespace: str
    creation_timestamp: _timestamp_pb2.Timestamp
    labels: _containers.ScalarMap[str, str]
    annotations: _containers.ScalarMap[str, str]
    def __init__(
        self,
        name: _Optional[str] = ...,
        namespace: _Optional[str] = ...,
        creation_timestamp: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        labels: _Optional[_Mapping[str, str]] = ...,
        annotations: _Optional[_Mapping[str, str]] = ...,
    ) -> None: ...

class ArgoWorkflowParameter(_message.Message):
    __slots__ = ("name", "value", "default")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_FIELD_NUMBER: _ClassVar[int]
    name: str
    value: str
    default: str
    def __init__(
        self,
        name: _Optional[str] = ...,
        value: _Optional[str] = ...,
        default: _Optional[str] = ...,
    ) -> None: ...

class ArgoWorkflowArguments(_message.Message):
    __slots__ = ("parameters",)
    PARAMETERS_FIELD_NUMBER: _ClassVar[int]
    parameters: _containers.RepeatedCompositeFieldContainer[ArgoWorkflowParameter]
    def __init__(
        self,
        parameters: _Optional[_Iterable[_Union[ArgoWorkflowParameter, _Mapping]]] = ...,
    ) -> None: ...

class ArgoWorkflowSpec(_message.Message):
    __slots__ = ("arguments", "entrypoint", "service_account_name")
    ARGUMENTS_FIELD_NUMBER: _ClassVar[int]
    ENTRYPOINT_FIELD_NUMBER: _ClassVar[int]
    SERVICE_ACCOUNT_NAME_FIELD_NUMBER: _ClassVar[int]
    arguments: ArgoWorkflowArguments
    entrypoint: str
    service_account_name: str
    def __init__(
        self,
        arguments: _Optional[_Union[ArgoWorkflowArguments, _Mapping]] = ...,
        entrypoint: _Optional[str] = ...,
        service_account_name: _Optional[str] = ...,
    ) -> None: ...

class ArgoWorkflowNodeOutputParameter(_message.Message):
    __slots__ = ("name", "value")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    name: str
    value: str
    def __init__(
        self, name: _Optional[str] = ..., value: _Optional[str] = ...
    ) -> None: ...

class ArgoWorkflowNodeOutputs(_message.Message):
    __slots__ = ("parameters",)
    PARAMETERS_FIELD_NUMBER: _ClassVar[int]
    parameters: _containers.RepeatedCompositeFieldContainer[
        ArgoWorkflowNodeOutputParameter
    ]
    def __init__(
        self,
        parameters: _Optional[
            _Iterable[_Union[ArgoWorkflowNodeOutputParameter, _Mapping]]
        ] = ...,
    ) -> None: ...

class ArgoWorkflowNodeInputParameter(_message.Message):
    __slots__ = ("name", "value")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    name: str
    value: str
    def __init__(
        self, name: _Optional[str] = ..., value: _Optional[str] = ...
    ) -> None: ...

class ArgoWorkflowNodeInputs(_message.Message):
    __slots__ = ("parameters",)
    PARAMETERS_FIELD_NUMBER: _ClassVar[int]
    parameters: _containers.RepeatedCompositeFieldContainer[
        ArgoWorkflowNodeInputParameter
    ]
    def __init__(
        self,
        parameters: _Optional[
            _Iterable[_Union[ArgoWorkflowNodeInputParameter, _Mapping]]
        ] = ...,
    ) -> None: ...

class ArgoWorkflowResourcesDuration(_message.Message):
    __slots__ = ("cpu", "memory", "storage", "ephemeral_storage")
    CPU_FIELD_NUMBER: _ClassVar[int]
    MEMORY_FIELD_NUMBER: _ClassVar[int]
    STORAGE_FIELD_NUMBER: _ClassVar[int]
    EPHEMERAL_STORAGE_FIELD_NUMBER: _ClassVar[int]
    cpu: int
    memory: int
    storage: int
    ephemeral_storage: int
    def __init__(
        self,
        cpu: _Optional[int] = ...,
        memory: _Optional[int] = ...,
        storage: _Optional[int] = ...,
        ephemeral_storage: _Optional[int] = ...,
    ) -> None: ...

class ArgoWorkflowNodeStatus(_message.Message):
    __slots__ = (
        "id",
        "name",
        "display_name",
        "type",
        "phase",
        "message",
        "started_at",
        "finished_at",
        "children",
        "outputs",
        "progress",
        "resources_duration",
        "template_name",
        "template_scope",
        "outbound_nodes",
        "inputs",
        "boundary_id",
    )
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    FINISHED_AT_FIELD_NUMBER: _ClassVar[int]
    CHILDREN_FIELD_NUMBER: _ClassVar[int]
    OUTPUTS_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    RESOURCES_DURATION_FIELD_NUMBER: _ClassVar[int]
    TEMPLATE_NAME_FIELD_NUMBER: _ClassVar[int]
    TEMPLATE_SCOPE_FIELD_NUMBER: _ClassVar[int]
    OUTBOUND_NODES_FIELD_NUMBER: _ClassVar[int]
    INPUTS_FIELD_NUMBER: _ClassVar[int]
    BOUNDARY_ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    display_name: str
    type: str
    phase: str
    message: str
    started_at: _timestamp_pb2.Timestamp
    finished_at: _timestamp_pb2.Timestamp
    children: _containers.RepeatedScalarFieldContainer[str]
    outputs: ArgoWorkflowNodeOutputs
    progress: str
    resources_duration: ArgoWorkflowResourcesDuration
    template_name: str
    template_scope: str
    outbound_nodes: _containers.RepeatedScalarFieldContainer[str]
    inputs: ArgoWorkflowNodeInputs
    boundary_id: str
    def __init__(
        self,
        id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        display_name: _Optional[str] = ...,
        type: _Optional[str] = ...,
        phase: _Optional[str] = ...,
        message: _Optional[str] = ...,
        started_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        finished_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        children: _Optional[_Iterable[str]] = ...,
        outputs: _Optional[_Union[ArgoWorkflowNodeOutputs, _Mapping]] = ...,
        progress: _Optional[str] = ...,
        resources_duration: _Optional[
            _Union[ArgoWorkflowResourcesDuration, _Mapping]
        ] = ...,
        template_name: _Optional[str] = ...,
        template_scope: _Optional[str] = ...,
        outbound_nodes: _Optional[_Iterable[str]] = ...,
        inputs: _Optional[_Union[ArgoWorkflowNodeInputs, _Mapping]] = ...,
        boundary_id: _Optional[str] = ...,
    ) -> None: ...

class ArgoWorkflowStatus(_message.Message):
    __slots__ = (
        "phase",
        "started_at",
        "finished_at",
        "message",
        "nodes",
        "progress",
        "resources_duration",
    )
    class NodesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: ArgoWorkflowNodeStatus
        def __init__(
            self,
            key: _Optional[str] = ...,
            value: _Optional[_Union[ArgoWorkflowNodeStatus, _Mapping]] = ...,
        ) -> None: ...

    PHASE_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    FINISHED_AT_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    NODES_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    RESOURCES_DURATION_FIELD_NUMBER: _ClassVar[int]
    phase: str
    started_at: _timestamp_pb2.Timestamp
    finished_at: _timestamp_pb2.Timestamp
    message: str
    nodes: _containers.MessageMap[str, ArgoWorkflowNodeStatus]
    progress: str
    resources_duration: ArgoWorkflowResourcesDuration
    def __init__(
        self,
        phase: _Optional[str] = ...,
        started_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        finished_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        message: _Optional[str] = ...,
        nodes: _Optional[_Mapping[str, ArgoWorkflowNodeStatus]] = ...,
        progress: _Optional[str] = ...,
        resources_duration: _Optional[
            _Union[ArgoWorkflowResourcesDuration, _Mapping]
        ] = ...,
    ) -> None: ...

class ArgoWorkflow(_message.Message):
    __slots__ = ("metadata", "status", "spec", "image_destinations")
    METADATA_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SPEC_FIELD_NUMBER: _ClassVar[int]
    IMAGE_DESTINATIONS_FIELD_NUMBER: _ClassVar[int]
    metadata: ArgoWorkflowMetadata
    status: ArgoWorkflowStatus
    spec: ArgoWorkflowSpec
    image_destinations: _containers.RepeatedScalarFieldContainer[str]
    def __init__(
        self,
        metadata: _Optional[_Union[ArgoWorkflowMetadata, _Mapping]] = ...,
        status: _Optional[_Union[ArgoWorkflowStatus, _Mapping]] = ...,
        spec: _Optional[_Union[ArgoWorkflowSpec, _Mapping]] = ...,
        image_destinations: _Optional[_Iterable[str]] = ...,
    ) -> None: ...
