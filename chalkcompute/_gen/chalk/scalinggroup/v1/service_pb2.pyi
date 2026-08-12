from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
from chalkcompute._gen.chalk.container.v1 import service_pb2 as _service_pb2
from chalkcompute._gen.chalk.flags.v1 import flags_pb2 as _flags_pb2
from google.protobuf import struct_pb2 as _struct_pb2
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

class ScalingGroupSortColumn(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SCALING_GROUP_SORT_COLUMN_UNSPECIFIED: _ClassVar[ScalingGroupSortColumn]
    SCALING_GROUP_SORT_COLUMN_CREATED_AT: _ClassVar[ScalingGroupSortColumn]
    SCALING_GROUP_SORT_COLUMN_UPDATED_AT: _ClassVar[ScalingGroupSortColumn]

class ScalingGroupSortOrder(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SCALING_GROUP_SORT_ORDER_UNSPECIFIED: _ClassVar[ScalingGroupSortOrder]
    SCALING_GROUP_SORT_ORDER_DESC: _ClassVar[ScalingGroupSortOrder]
    SCALING_GROUP_SORT_ORDER_ASC: _ClassVar[ScalingGroupSortOrder]

class ScalingGroupVisibility(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SCALING_GROUP_VISIBILITY_UNSPECIFIED: _ClassVar[ScalingGroupVisibility]
    SCALING_GROUP_VISIBILITY_ACTIVE: _ClassVar[ScalingGroupVisibility]
    SCALING_GROUP_VISIBILITY_ARCHIVED: _ClassVar[ScalingGroupVisibility]

SCALING_GROUP_SORT_COLUMN_UNSPECIFIED: ScalingGroupSortColumn
SCALING_GROUP_SORT_COLUMN_CREATED_AT: ScalingGroupSortColumn
SCALING_GROUP_SORT_COLUMN_UPDATED_AT: ScalingGroupSortColumn
SCALING_GROUP_SORT_ORDER_UNSPECIFIED: ScalingGroupSortOrder
SCALING_GROUP_SORT_ORDER_DESC: ScalingGroupSortOrder
SCALING_GROUP_SORT_ORDER_ASC: ScalingGroupSortOrder
SCALING_GROUP_VISIBILITY_UNSPECIFIED: ScalingGroupVisibility
SCALING_GROUP_VISIBILITY_ACTIVE: ScalingGroupVisibility
SCALING_GROUP_VISIBILITY_ARCHIVED: ScalingGroupVisibility

class ScalingSpec(_message.Message):
    __slots__ = (
        "min_replicas",
        "max_replicas",
        "target_cpu_utilization_percentage",
        "shutdown_delay_seconds",
        "window_seconds",
        "function_queue_depth_trigger",
        "gpu_utilization_trigger",
        "cron_scaling_trigger",
    )
    MIN_REPLICAS_FIELD_NUMBER: _ClassVar[int]
    MAX_REPLICAS_FIELD_NUMBER: _ClassVar[int]
    TARGET_CPU_UTILIZATION_PERCENTAGE_FIELD_NUMBER: _ClassVar[int]
    SHUTDOWN_DELAY_SECONDS_FIELD_NUMBER: _ClassVar[int]
    WINDOW_SECONDS_FIELD_NUMBER: _ClassVar[int]
    FUNCTION_QUEUE_DEPTH_TRIGGER_FIELD_NUMBER: _ClassVar[int]
    GPU_UTILIZATION_TRIGGER_FIELD_NUMBER: _ClassVar[int]
    CRON_SCALING_TRIGGER_FIELD_NUMBER: _ClassVar[int]
    min_replicas: int
    max_replicas: int
    target_cpu_utilization_percentage: int
    shutdown_delay_seconds: int
    window_seconds: int
    function_queue_depth_trigger: FunctionQueueDepthScalingTrigger
    gpu_utilization_trigger: GpuUtilizationScalingTrigger
    cron_scaling_trigger: CronScalingTrigger
    def __init__(
        self,
        min_replicas: _Optional[int] = ...,
        max_replicas: _Optional[int] = ...,
        target_cpu_utilization_percentage: _Optional[int] = ...,
        shutdown_delay_seconds: _Optional[int] = ...,
        window_seconds: _Optional[int] = ...,
        function_queue_depth_trigger: _Optional[
            _Union[FunctionQueueDepthScalingTrigger, _Mapping]
        ] = ...,
        gpu_utilization_trigger: _Optional[
            _Union[GpuUtilizationScalingTrigger, _Mapping]
        ] = ...,
        cron_scaling_trigger: _Optional[_Union[CronScalingTrigger, _Mapping]] = ...,
    ) -> None: ...

class FunctionQueueDepthScalingTrigger(_message.Message):
    __slots__ = ("function_name", "target_queue_depth")
    FUNCTION_NAME_FIELD_NUMBER: _ClassVar[int]
    TARGET_QUEUE_DEPTH_FIELD_NUMBER: _ClassVar[int]
    function_name: str
    target_queue_depth: int
    def __init__(
        self,
        function_name: _Optional[str] = ...,
        target_queue_depth: _Optional[int] = ...,
    ) -> None: ...

class GpuUtilizationScalingTrigger(_message.Message):
    __slots__ = ("target_utilization_percentage",)
    TARGET_UTILIZATION_PERCENTAGE_FIELD_NUMBER: _ClassVar[int]
    target_utilization_percentage: int
    def __init__(self, target_utilization_percentage: _Optional[int] = ...) -> None: ...

class CronScalingTrigger(_message.Message):
    __slots__ = ("timezone", "windows")
    TIMEZONE_FIELD_NUMBER: _ClassVar[int]
    WINDOWS_FIELD_NUMBER: _ClassVar[int]
    timezone: str
    windows: _containers.RepeatedCompositeFieldContainer[CronScalingWindow]
    def __init__(
        self,
        timezone: _Optional[str] = ...,
        windows: _Optional[_Iterable[_Union[CronScalingWindow, _Mapping]]] = ...,
    ) -> None: ...

class CronScalingWindow(_message.Message):
    __slots__ = ("start", "end", "desired_replicas")
    START_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    DESIRED_REPLICAS_FIELD_NUMBER: _ClassVar[int]
    start: str
    end: str
    desired_replicas: int
    def __init__(
        self,
        start: _Optional[str] = ...,
        end: _Optional[str] = ...,
        desired_replicas: _Optional[int] = ...,
    ) -> None: ...

class ScalingGroupSpec(_message.Message):
    __slots__ = ("container_spec", "scaling_spec")
    CONTAINER_SPEC_FIELD_NUMBER: _ClassVar[int]
    SCALING_SPEC_FIELD_NUMBER: _ClassVar[int]
    container_spec: _service_pb2.ChalkContainerSpec
    scaling_spec: ScalingSpec
    def __init__(
        self,
        container_spec: _Optional[
            _Union[_service_pb2.ChalkContainerSpec, _Mapping]
        ] = ...,
        scaling_spec: _Optional[_Union[ScalingSpec, _Mapping]] = ...,
    ) -> None: ...

class ScalingGroupResponse(_message.Message):
    __slots__ = (
        "id",
        "name",
        "revision_id",
        "status",
        "status_message",
        "spec",
        "created_at",
        "updated_at",
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
            self,
            key: _Optional[str] = ...,
            value: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ...,
        ) -> None: ...

    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    SPEC_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    DELETED_AT_FIELD_NUMBER: _ClassVar[int]
    WEB_URL_FIELD_NUMBER: _ClassVar[int]
    READY_REPLICAS_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_REPLICAS_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    revision_id: str
    status: str
    status_message: str
    spec: ScalingGroupSpec
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    deleted_at: _timestamp_pb2.Timestamp
    web_url: str
    ready_replicas: int
    available_replicas: int
    metadata: _containers.MessageMap[str, _struct_pb2.Value]
    def __init__(
        self,
        id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        revision_id: _Optional[str] = ...,
        status: _Optional[str] = ...,
        status_message: _Optional[str] = ...,
        spec: _Optional[_Union[ScalingGroupSpec, _Mapping]] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        updated_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
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
    def __init__(
        self, spec: _Optional[_Union[ScalingGroupSpec, _Mapping]] = ...
    ) -> None: ...

class CreateScalingGroupResponse(_message.Message):
    __slots__ = ("scaling_group",)
    SCALING_GROUP_FIELD_NUMBER: _ClassVar[int]
    scaling_group: ScalingGroupResponse
    def __init__(
        self, scaling_group: _Optional[_Union[ScalingGroupResponse, _Mapping]] = ...
    ) -> None: ...

class GetScalingGroupRequest(_message.Message):
    __slots__ = ("id", "name", "visibility")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    VISIBILITY_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    visibility: _containers.RepeatedScalarFieldContainer[ScalingGroupVisibility]
    def __init__(
        self,
        id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        visibility: _Optional[_Iterable[_Union[ScalingGroupVisibility, str]]] = ...,
    ) -> None: ...

class GetScalingGroupResponse(_message.Message):
    __slots__ = ("scaling_group",)
    SCALING_GROUP_FIELD_NUMBER: _ClassVar[int]
    scaling_group: ScalingGroupResponse
    def __init__(
        self, scaling_group: _Optional[_Union[ScalingGroupResponse, _Mapping]] = ...
    ) -> None: ...

class ListScalingGroupsRequest(_message.Message):
    __slots__ = ("cursor", "limit", "filters", "search", "sort_column", "sort_order")
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    FILTERS_FIELD_NUMBER: _ClassVar[int]
    SEARCH_FIELD_NUMBER: _ClassVar[int]
    SORT_COLUMN_FIELD_NUMBER: _ClassVar[int]
    SORT_ORDER_FIELD_NUMBER: _ClassVar[int]
    cursor: str
    limit: int
    filters: ListScalingGroupsFilters
    search: str
    sort_column: ScalingGroupSortColumn
    sort_order: ScalingGroupSortOrder
    def __init__(
        self,
        cursor: _Optional[str] = ...,
        limit: _Optional[int] = ...,
        filters: _Optional[_Union[ListScalingGroupsFilters, _Mapping]] = ...,
        search: _Optional[str] = ...,
        sort_column: _Optional[_Union[ScalingGroupSortColumn, str]] = ...,
        sort_order: _Optional[_Union[ScalingGroupSortOrder, str]] = ...,
    ) -> None: ...

class ListScalingGroupsFilters(_message.Message):
    __slots__ = ("statuses", "visibility", "images")
    STATUSES_FIELD_NUMBER: _ClassVar[int]
    VISIBILITY_FIELD_NUMBER: _ClassVar[int]
    IMAGES_FIELD_NUMBER: _ClassVar[int]
    statuses: _containers.RepeatedScalarFieldContainer[str]
    visibility: _containers.RepeatedScalarFieldContainer[ScalingGroupVisibility]
    images: _containers.RepeatedScalarFieldContainer[str]
    def __init__(
        self,
        statuses: _Optional[_Iterable[str]] = ...,
        visibility: _Optional[_Iterable[_Union[ScalingGroupVisibility, str]]] = ...,
        images: _Optional[_Iterable[str]] = ...,
    ) -> None: ...

class ListScalingGroupsResponse(_message.Message):
    __slots__ = ("scaling_groups", "next_cursor")
    SCALING_GROUPS_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    scaling_groups: _containers.RepeatedCompositeFieldContainer[ScalingGroupResponse]
    next_cursor: str
    def __init__(
        self,
        scaling_groups: _Optional[
            _Iterable[_Union[ScalingGroupResponse, _Mapping]]
        ] = ...,
        next_cursor: _Optional[str] = ...,
    ) -> None: ...

class ScalingGroupRevisionResponse(_message.Message):
    __slots__ = (
        "id",
        "scaling_group_id",
        "scaling_group_name",
        "status",
        "status_message",
        "spec",
        "created_at",
        "metadata",
        "latest",
    )
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: _struct_pb2.Value
        def __init__(
            self,
            key: _Optional[str] = ...,
            value: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ...,
        ) -> None: ...

    ID_FIELD_NUMBER: _ClassVar[int]
    SCALING_GROUP_ID_FIELD_NUMBER: _ClassVar[int]
    SCALING_GROUP_NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    SPEC_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    LATEST_FIELD_NUMBER: _ClassVar[int]
    id: str
    scaling_group_id: str
    scaling_group_name: str
    status: str
    status_message: str
    spec: ScalingGroupSpec
    created_at: _timestamp_pb2.Timestamp
    metadata: _containers.MessageMap[str, _struct_pb2.Value]
    latest: bool
    def __init__(
        self,
        id: _Optional[str] = ...,
        scaling_group_id: _Optional[str] = ...,
        scaling_group_name: _Optional[str] = ...,
        status: _Optional[str] = ...,
        status_message: _Optional[str] = ...,
        spec: _Optional[_Union[ScalingGroupSpec, _Mapping]] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        metadata: _Optional[_Mapping[str, _struct_pb2.Value]] = ...,
        latest: bool = ...,
    ) -> None: ...

class GetScalingGroupRevisionRequest(_message.Message):
    __slots__ = ("scaling_group_id", "scaling_group_name", "revision_id")
    SCALING_GROUP_ID_FIELD_NUMBER: _ClassVar[int]
    SCALING_GROUP_NAME_FIELD_NUMBER: _ClassVar[int]
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    scaling_group_id: str
    scaling_group_name: str
    revision_id: str
    def __init__(
        self,
        scaling_group_id: _Optional[str] = ...,
        scaling_group_name: _Optional[str] = ...,
        revision_id: _Optional[str] = ...,
    ) -> None: ...

class GetScalingGroupRevisionResponse(_message.Message):
    __slots__ = ("revision",)
    REVISION_FIELD_NUMBER: _ClassVar[int]
    revision: ScalingGroupRevisionResponse
    def __init__(
        self, revision: _Optional[_Union[ScalingGroupRevisionResponse, _Mapping]] = ...
    ) -> None: ...

class ListScalingGroupRevisionsRequest(_message.Message):
    __slots__ = ("scaling_group_id", "scaling_group_name", "cursor", "limit", "filters")
    SCALING_GROUP_ID_FIELD_NUMBER: _ClassVar[int]
    SCALING_GROUP_NAME_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    FILTERS_FIELD_NUMBER: _ClassVar[int]
    scaling_group_id: str
    scaling_group_name: str
    cursor: str
    limit: int
    filters: ListScalingGroupRevisionsFilters
    def __init__(
        self,
        scaling_group_id: _Optional[str] = ...,
        scaling_group_name: _Optional[str] = ...,
        cursor: _Optional[str] = ...,
        limit: _Optional[int] = ...,
        filters: _Optional[_Union[ListScalingGroupRevisionsFilters, _Mapping]] = ...,
    ) -> None: ...

class ListScalingGroupRevisionsFilters(_message.Message):
    __slots__ = ("visibility",)
    VISIBILITY_FIELD_NUMBER: _ClassVar[int]
    visibility: _containers.RepeatedScalarFieldContainer[ScalingGroupVisibility]
    def __init__(
        self,
        visibility: _Optional[_Iterable[_Union[ScalingGroupVisibility, str]]] = ...,
    ) -> None: ...

class ListScalingGroupRevisionsResponse(_message.Message):
    __slots__ = ("revisions", "next_cursor")
    REVISIONS_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    revisions: _containers.RepeatedCompositeFieldContainer[ScalingGroupRevisionResponse]
    next_cursor: str
    def __init__(
        self,
        revisions: _Optional[
            _Iterable[_Union[ScalingGroupRevisionResponse, _Mapping]]
        ] = ...,
        next_cursor: _Optional[str] = ...,
    ) -> None: ...

class DeleteScalingGroupRequest(_message.Message):
    __slots__ = ("id", "name")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    def __init__(
        self, id: _Optional[str] = ..., name: _Optional[str] = ...
    ) -> None: ...

class DeleteScalingGroupResponse(_message.Message):
    __slots__ = ("scaling_group",)
    SCALING_GROUP_FIELD_NUMBER: _ClassVar[int]
    scaling_group: ScalingGroupResponse
    def __init__(
        self, scaling_group: _Optional[_Union[ScalingGroupResponse, _Mapping]] = ...
    ) -> None: ...

class UpdateScalingGroupStatusRequest(_message.Message):
    __slots__ = ("scaling_group_id", "status", "status_message")
    SCALING_GROUP_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    scaling_group_id: str
    status: str
    status_message: str
    def __init__(
        self,
        scaling_group_id: _Optional[str] = ...,
        status: _Optional[str] = ...,
        status_message: _Optional[str] = ...,
    ) -> None: ...

class BatchUpdateScalingGroupStatusRequest(_message.Message):
    __slots__ = ("updates",)
    UPDATES_FIELD_NUMBER: _ClassVar[int]
    updates: _containers.RepeatedCompositeFieldContainer[
        UpdateScalingGroupStatusRequest
    ]
    def __init__(
        self,
        updates: _Optional[
            _Iterable[_Union[UpdateScalingGroupStatusRequest, _Mapping]]
        ] = ...,
    ) -> None: ...

class BatchUpdateScalingGroupStatusResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
