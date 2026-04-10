from chalk._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
from chalk._gen.chalk.container.v1 import service_pb2 as _service_pb2
from google.protobuf import struct_pb2 as _struct_pb2
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

class ScalingSpec(_message.Message):
    __slots__ = ("min_replicas", "max_replicas", "target_cpu_utilization_percentage", "shutdown_delay_seconds")
    MIN_REPLICAS_FIELD_NUMBER: _ClassVar[int]
    MAX_REPLICAS_FIELD_NUMBER: _ClassVar[int]
    TARGET_CPU_UTILIZATION_PERCENTAGE_FIELD_NUMBER: _ClassVar[int]
    SHUTDOWN_DELAY_SECONDS_FIELD_NUMBER: _ClassVar[int]
    min_replicas: int
    max_replicas: int
    target_cpu_utilization_percentage: int
    shutdown_delay_seconds: int
    def __init__(
        self,
        min_replicas: _Optional[int] = ...,
        max_replicas: _Optional[int] = ...,
        target_cpu_utilization_percentage: _Optional[int] = ...,
        shutdown_delay_seconds: _Optional[int] = ...,
    ) -> None: ...

class ScalingGroupSpec(_message.Message):
    __slots__ = ("container_spec", "scaling_spec")
    CONTAINER_SPEC_FIELD_NUMBER: _ClassVar[int]
    SCALING_SPEC_FIELD_NUMBER: _ClassVar[int]
    container_spec: _service_pb2.ChalkContainerSpec
    scaling_spec: ScalingSpec
    def __init__(
        self,
        container_spec: _Optional[_Union[_service_pb2.ChalkContainerSpec, _Mapping]] = ...,
        scaling_spec: _Optional[_Union[ScalingSpec, _Mapping]] = ...,
    ) -> None: ...

class ScalingGroupResponse(_message.Message):
    __slots__ = (
        "id",
        "name",
        "status",
        "status_message",
        "spec",
        "created_at",
        "deleted_at",
        "web_url",
        "ready_replicas",
        "available_replicas",
        "metadata",
    )
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: _struct_pb2.Value
        def __init__(
            self, key: _Optional[str] = ..., value: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ...
        ) -> None: ...

    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    SPEC_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    DELETED_AT_FIELD_NUMBER: _ClassVar[int]
    WEB_URL_FIELD_NUMBER: _ClassVar[int]
    READY_REPLICAS_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_REPLICAS_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    status: str
    status_message: str
    spec: ScalingGroupSpec
    created_at: _timestamp_pb2.Timestamp
    deleted_at: _timestamp_pb2.Timestamp
    web_url: str
    ready_replicas: int
    available_replicas: int
    metadata: _containers.MessageMap[str, _struct_pb2.Value]
    def __init__(
        self,
        id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        status: _Optional[str] = ...,
        status_message: _Optional[str] = ...,
        spec: _Optional[_Union[ScalingGroupSpec, _Mapping]] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        deleted_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        web_url: _Optional[str] = ...,
        ready_replicas: _Optional[int] = ...,
        available_replicas: _Optional[int] = ...,
        metadata: _Optional[_Mapping[str, _struct_pb2.Value]] = ...,
    ) -> None: ...

class CreateScalingGroupRequest(_message.Message):
    __slots__ = ("spec",)
    SPEC_FIELD_NUMBER: _ClassVar[int]
    spec: ScalingGroupSpec
    def __init__(self, spec: _Optional[_Union[ScalingGroupSpec, _Mapping]] = ...) -> None: ...

class CreateScalingGroupResponse(_message.Message):
    __slots__ = ("scaling_group",)
    SCALING_GROUP_FIELD_NUMBER: _ClassVar[int]
    scaling_group: ScalingGroupResponse
    def __init__(self, scaling_group: _Optional[_Union[ScalingGroupResponse, _Mapping]] = ...) -> None: ...

class GetScalingGroupRequest(_message.Message):
    __slots__ = ("id", "name")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class GetScalingGroupResponse(_message.Message):
    __slots__ = ("scaling_group",)
    SCALING_GROUP_FIELD_NUMBER: _ClassVar[int]
    scaling_group: ScalingGroupResponse
    def __init__(self, scaling_group: _Optional[_Union[ScalingGroupResponse, _Mapping]] = ...) -> None: ...

class ListScalingGroupsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListScalingGroupsResponse(_message.Message):
    __slots__ = ("scaling_groups",)
    SCALING_GROUPS_FIELD_NUMBER: _ClassVar[int]
    scaling_groups: _containers.RepeatedCompositeFieldContainer[ScalingGroupResponse]
    def __init__(self, scaling_groups: _Optional[_Iterable[_Union[ScalingGroupResponse, _Mapping]]] = ...) -> None: ...

class DeleteScalingGroupRequest(_message.Message):
    __slots__ = ("id", "name")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class DeleteScalingGroupResponse(_message.Message):
    __slots__ = ("scaling_group",)
    SCALING_GROUP_FIELD_NUMBER: _ClassVar[int]
    scaling_group: ScalingGroupResponse
    def __init__(self, scaling_group: _Optional[_Union[ScalingGroupResponse, _Mapping]] = ...) -> None: ...

class UpdateScalingGroupStatusRequest(_message.Message):
    __slots__ = ("scaling_group_id", "status", "status_message")
    SCALING_GROUP_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    scaling_group_id: str
    status: str
    status_message: str
    def __init__(
        self, scaling_group_id: _Optional[str] = ..., status: _Optional[str] = ..., status_message: _Optional[str] = ...
    ) -> None: ...

class BatchUpdateScalingGroupStatusRequest(_message.Message):
    __slots__ = ("updates",)
    UPDATES_FIELD_NUMBER: _ClassVar[int]
    updates: _containers.RepeatedCompositeFieldContainer[UpdateScalingGroupStatusRequest]
    def __init__(
        self, updates: _Optional[_Iterable[_Union[UpdateScalingGroupStatusRequest, _Mapping]]] = ...
    ) -> None: ...

class BatchUpdateScalingGroupStatusResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
