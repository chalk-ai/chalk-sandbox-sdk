from chalkcompute._gen.chalk.arrow.v1 import arrow_pb2 as _arrow_pb2
from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
from chalkcompute._gen.chalk.flags.v1 import flags_pb2 as _flags_pb2
from chalkcompute._gen.chalk.runtime.v1 import (
    remote_python_call_pb2 as _remote_python_call_pb2,
)
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb2 as _service_pb2
from chalkcompute._gen.chalk.volume.v2 import volume_pb2 as _volume_pb2
from google.protobuf import field_mask_pb2 as _field_mask_pb2
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

class RateLimitPer(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RATE_LIMIT_PER_UNSPECIFIED: _ClassVar[RateLimitPer]
    RATE_LIMIT_PER_SECOND: _ClassVar[RateLimitPer]
    RATE_LIMIT_PER_MINUTE: _ClassVar[RateLimitPer]
    RATE_LIMIT_PER_HOUR: _ClassVar[RateLimitPer]

class SerializationFormat(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SERIALIZATION_FORMAT_UNSPECIFIED: _ClassVar[SerializationFormat]
    SERIALIZATION_FORMAT_PYARROW: _ClassVar[SerializationFormat]

class TracingMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TRACING_MODE_UNSPECIFIED: _ClassVar[TracingMode]
    TRACING_MODE_PARENT_BASED_ALWAYS_OFF: _ClassVar[TracingMode]
    TRACING_MODE_PARENT_BASED_TRACE_ID_RATIO: _ClassVar[TracingMode]
    TRACING_MODE_ALWAYS_OFF: _ClassVar[TracingMode]

class ExternalFunctionSortColumn(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EXTERNAL_FUNCTION_SORT_COLUMN_UNSPECIFIED: _ClassVar[ExternalFunctionSortColumn]
    EXTERNAL_FUNCTION_SORT_COLUMN_CREATED_AT: _ClassVar[ExternalFunctionSortColumn]
    EXTERNAL_FUNCTION_SORT_COLUMN_UPDATED_AT: _ClassVar[ExternalFunctionSortColumn]

class ExternalFunctionSortOrder(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EXTERNAL_FUNCTION_SORT_ORDER_UNSPECIFIED: _ClassVar[ExternalFunctionSortOrder]
    EXTERNAL_FUNCTION_SORT_ORDER_DESC: _ClassVar[ExternalFunctionSortOrder]
    EXTERNAL_FUNCTION_SORT_ORDER_ASC: _ClassVar[ExternalFunctionSortOrder]

class ExternalFunctionVisibility(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EXTERNAL_FUNCTION_VISIBILITY_UNSPECIFIED: _ClassVar[ExternalFunctionVisibility]
    EXTERNAL_FUNCTION_VISIBILITY_ACTIVE: _ClassVar[ExternalFunctionVisibility]
    EXTERNAL_FUNCTION_VISIBILITY_ARCHIVED: _ClassVar[ExternalFunctionVisibility]

class ExternalFunctionScheduledRunStatus(
    int, metaclass=_enum_type_wrapper.EnumTypeWrapper
):
    __slots__ = ()
    EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_UNSPECIFIED: _ClassVar[
        ExternalFunctionScheduledRunStatus
    ]
    EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_SCHEDULED: _ClassVar[
        ExternalFunctionScheduledRunStatus
    ]
    EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_RUNNING: _ClassVar[
        ExternalFunctionScheduledRunStatus
    ]
    EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_COMPLETED: _ClassVar[
        ExternalFunctionScheduledRunStatus
    ]
    EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_FAILED: _ClassVar[
        ExternalFunctionScheduledRunStatus
    ]
    EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_CANCELED: _ClassVar[
        ExternalFunctionScheduledRunStatus
    ]
    EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_NOT_READY: _ClassVar[
        ExternalFunctionScheduledRunStatus
    ]
    EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_WAITING: _ClassVar[
        ExternalFunctionScheduledRunStatus
    ]

RATE_LIMIT_PER_UNSPECIFIED: RateLimitPer
RATE_LIMIT_PER_SECOND: RateLimitPer
RATE_LIMIT_PER_MINUTE: RateLimitPer
RATE_LIMIT_PER_HOUR: RateLimitPer
SERIALIZATION_FORMAT_UNSPECIFIED: SerializationFormat
SERIALIZATION_FORMAT_PYARROW: SerializationFormat
TRACING_MODE_UNSPECIFIED: TracingMode
TRACING_MODE_PARENT_BASED_ALWAYS_OFF: TracingMode
TRACING_MODE_PARENT_BASED_TRACE_ID_RATIO: TracingMode
TRACING_MODE_ALWAYS_OFF: TracingMode
EXTERNAL_FUNCTION_SORT_COLUMN_UNSPECIFIED: ExternalFunctionSortColumn
EXTERNAL_FUNCTION_SORT_COLUMN_CREATED_AT: ExternalFunctionSortColumn
EXTERNAL_FUNCTION_SORT_COLUMN_UPDATED_AT: ExternalFunctionSortColumn
EXTERNAL_FUNCTION_SORT_ORDER_UNSPECIFIED: ExternalFunctionSortOrder
EXTERNAL_FUNCTION_SORT_ORDER_DESC: ExternalFunctionSortOrder
EXTERNAL_FUNCTION_SORT_ORDER_ASC: ExternalFunctionSortOrder
EXTERNAL_FUNCTION_VISIBILITY_UNSPECIFIED: ExternalFunctionVisibility
EXTERNAL_FUNCTION_VISIBILITY_ACTIVE: ExternalFunctionVisibility
EXTERNAL_FUNCTION_VISIBILITY_ARCHIVED: ExternalFunctionVisibility
EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_UNSPECIFIED: ExternalFunctionScheduledRunStatus
EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_SCHEDULED: ExternalFunctionScheduledRunStatus
EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_RUNNING: ExternalFunctionScheduledRunStatus
EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_COMPLETED: ExternalFunctionScheduledRunStatus
EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_FAILED: ExternalFunctionScheduledRunStatus
EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_CANCELED: ExternalFunctionScheduledRunStatus
EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_NOT_READY: ExternalFunctionScheduledRunStatus
EXTERNAL_FUNCTION_SCHEDULED_RUN_STATUS_WAITING: ExternalFunctionScheduledRunStatus

class RetryPolicy(_message.Message):
    __slots__ = (
        "max_retries",
        "initial_backoff_ms",
        "backoff_multiplier",
        "max_backoff_ms",
        "key",
    )
    MAX_RETRIES_FIELD_NUMBER: _ClassVar[int]
    INITIAL_BACKOFF_MS_FIELD_NUMBER: _ClassVar[int]
    BACKOFF_MULTIPLIER_FIELD_NUMBER: _ClassVar[int]
    MAX_BACKOFF_MS_FIELD_NUMBER: _ClassVar[int]
    KEY_FIELD_NUMBER: _ClassVar[int]
    max_retries: int
    initial_backoff_ms: int
    backoff_multiplier: float
    max_backoff_ms: int
    key: str
    def __init__(
        self,
        max_retries: _Optional[int] = ...,
        initial_backoff_ms: _Optional[int] = ...,
        backoff_multiplier: _Optional[float] = ...,
        max_backoff_ms: _Optional[int] = ...,
        key: _Optional[str] = ...,
    ) -> None: ...

class RateLimitPolicy(_message.Message):
    __slots__ = ("rate", "per", "key")
    RATE_FIELD_NUMBER: _ClassVar[int]
    PER_FIELD_NUMBER: _ClassVar[int]
    KEY_FIELD_NUMBER: _ClassVar[int]
    rate: int
    per: RateLimitPer
    key: str
    def __init__(
        self,
        rate: _Optional[int] = ...,
        per: _Optional[_Union[RateLimitPer, str]] = ...,
        key: _Optional[str] = ...,
    ) -> None: ...

class ConcurrencyPolicy(_message.Message):
    __slots__ = ("max_concurrent", "key")
    MAX_CONCURRENT_FIELD_NUMBER: _ClassVar[int]
    KEY_FIELD_NUMBER: _ClassVar[int]
    max_concurrent: int
    key: str
    def __init__(
        self, max_concurrent: _Optional[int] = ..., key: _Optional[str] = ...
    ) -> None: ...

class QueuePolicy(_message.Message):
    __slots__ = ("max_items", "key")
    MAX_ITEMS_FIELD_NUMBER: _ClassVar[int]
    KEY_FIELD_NUMBER: _ClassVar[int]
    max_items: int
    key: str
    def __init__(
        self, max_items: _Optional[int] = ..., key: _Optional[str] = ...
    ) -> None: ...

class TracingPolicy(_message.Message):
    __slots__ = ("mode", "sample_rate")
    MODE_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_RATE_FIELD_NUMBER: _ClassVar[int]
    mode: TracingMode
    sample_rate: float
    def __init__(
        self,
        mode: _Optional[_Union[TracingMode, str]] = ...,
        sample_rate: _Optional[float] = ...,
    ) -> None: ...

class FunctionConfig(_message.Message):
    __slots__ = (
        "serialization_format",
        "options",
        "max_buffer_duration",
        "max_batching_size",
        "retry_policy",
        "rate_limit",
        "concurrency",
        "queue",
        "schedule",
        "tracing",
    )
    class OptionsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(
            self, key: _Optional[str] = ..., value: _Optional[str] = ...
        ) -> None: ...

    SERIALIZATION_FORMAT_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    MAX_BUFFER_DURATION_FIELD_NUMBER: _ClassVar[int]
    MAX_BATCHING_SIZE_FIELD_NUMBER: _ClassVar[int]
    RETRY_POLICY_FIELD_NUMBER: _ClassVar[int]
    RATE_LIMIT_FIELD_NUMBER: _ClassVar[int]
    CONCURRENCY_FIELD_NUMBER: _ClassVar[int]
    QUEUE_FIELD_NUMBER: _ClassVar[int]
    SCHEDULE_FIELD_NUMBER: _ClassVar[int]
    TRACING_FIELD_NUMBER: _ClassVar[int]
    serialization_format: SerializationFormat
    options: _containers.ScalarMap[str, str]
    max_buffer_duration: int
    max_batching_size: int
    retry_policy: RetryPolicy
    rate_limit: RateLimitPolicy
    concurrency: ConcurrencyPolicy
    queue: QueuePolicy
    schedule: str
    tracing: TracingPolicy
    def __init__(
        self,
        serialization_format: _Optional[_Union[SerializationFormat, str]] = ...,
        options: _Optional[_Mapping[str, str]] = ...,
        max_buffer_duration: _Optional[int] = ...,
        max_batching_size: _Optional[int] = ...,
        retry_policy: _Optional[_Union[RetryPolicy, _Mapping]] = ...,
        rate_limit: _Optional[_Union[RateLimitPolicy, _Mapping]] = ...,
        concurrency: _Optional[_Union[ConcurrencyPolicy, _Mapping]] = ...,
        queue: _Optional[_Union[QueuePolicy, _Mapping]] = ...,
        schedule: _Optional[str] = ...,
        tracing: _Optional[_Union[TracingPolicy, _Mapping]] = ...,
    ) -> None: ...

class ExternalFunctionVersion(_message.Message):
    __slots__ = (
        "id",
        "function_name",
        "version",
        "input_arrow_schema",
        "output_arrow_schema",
        "scaling_group_name",
        "scaling_group_revision_id",
        "created_at",
        "config",
        "deleted_at",
    )
    ID_FIELD_NUMBER: _ClassVar[int]
    FUNCTION_NAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    INPUT_ARROW_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_ARROW_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    SCALING_GROUP_NAME_FIELD_NUMBER: _ClassVar[int]
    SCALING_GROUP_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    DELETED_AT_FIELD_NUMBER: _ClassVar[int]
    id: str
    function_name: str
    version: int
    input_arrow_schema: _arrow_pb2.Schema
    output_arrow_schema: _arrow_pb2.Schema
    scaling_group_name: str
    scaling_group_revision_id: str
    created_at: _timestamp_pb2.Timestamp
    config: FunctionConfig
    deleted_at: _timestamp_pb2.Timestamp
    def __init__(
        self,
        id: _Optional[str] = ...,
        function_name: _Optional[str] = ...,
        version: _Optional[int] = ...,
        input_arrow_schema: _Optional[_Union[_arrow_pb2.Schema, _Mapping]] = ...,
        output_arrow_schema: _Optional[_Union[_arrow_pb2.Schema, _Mapping]] = ...,
        scaling_group_name: _Optional[str] = ...,
        scaling_group_revision_id: _Optional[str] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        config: _Optional[_Union[FunctionConfig, _Mapping]] = ...,
        deleted_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
    ) -> None: ...

class CreateExternalFunctionVersionRequest(_message.Message):
    __slots__ = (
        "function_name",
        "input_arrow_schema",
        "output_arrow_schema",
        "spec",
        "config",
        "volume_commits",
    )
    FUNCTION_NAME_FIELD_NUMBER: _ClassVar[int]
    INPUT_ARROW_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_ARROW_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    SPEC_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    VOLUME_COMMITS_FIELD_NUMBER: _ClassVar[int]
    function_name: str
    input_arrow_schema: _arrow_pb2.Schema
    output_arrow_schema: _arrow_pb2.Schema
    spec: _service_pb2.ScalingGroupSpec
    config: FunctionConfig
    volume_commits: _containers.RepeatedCompositeFieldContainer[
        _volume_pb2.CommitIntent
    ]
    def __init__(
        self,
        function_name: _Optional[str] = ...,
        input_arrow_schema: _Optional[_Union[_arrow_pb2.Schema, _Mapping]] = ...,
        output_arrow_schema: _Optional[_Union[_arrow_pb2.Schema, _Mapping]] = ...,
        spec: _Optional[_Union[_service_pb2.ScalingGroupSpec, _Mapping]] = ...,
        config: _Optional[_Union[FunctionConfig, _Mapping]] = ...,
        volume_commits: _Optional[
            _Iterable[_Union[_volume_pb2.CommitIntent, _Mapping]]
        ] = ...,
    ) -> None: ...

class CreateExternalFunctionVersionResponse(_message.Message):
    __slots__ = ("external_function_version", "scaling_group")
    EXTERNAL_FUNCTION_VERSION_FIELD_NUMBER: _ClassVar[int]
    SCALING_GROUP_FIELD_NUMBER: _ClassVar[int]
    external_function_version: ExternalFunctionVersion
    scaling_group: _service_pb2.ScalingGroupResponse
    def __init__(
        self,
        external_function_version: _Optional[
            _Union[ExternalFunctionVersion, _Mapping]
        ] = ...,
        scaling_group: _Optional[
            _Union[_service_pb2.ScalingGroupResponse, _Mapping]
        ] = ...,
    ) -> None: ...

class ExternalFunctionVersionKey(_message.Message):
    __slots__ = ("function_name", "version")
    FUNCTION_NAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    function_name: str
    version: int
    def __init__(
        self, function_name: _Optional[str] = ..., version: _Optional[int] = ...
    ) -> None: ...

class GetExternalFunctionVersionRequest(_message.Message):
    __slots__ = (
        "id",
        "key",
        "include_scaling_group",
        "include_active_schedule",
        "visibility",
        "include_deleted",
    )
    ID_FIELD_NUMBER: _ClassVar[int]
    KEY_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_SCALING_GROUP_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_ACTIVE_SCHEDULE_FIELD_NUMBER: _ClassVar[int]
    VISIBILITY_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_DELETED_FIELD_NUMBER: _ClassVar[int]
    id: str
    key: ExternalFunctionVersionKey
    include_scaling_group: bool
    include_active_schedule: bool
    visibility: _containers.RepeatedScalarFieldContainer[ExternalFunctionVisibility]
    include_deleted: bool
    def __init__(
        self,
        id: _Optional[str] = ...,
        key: _Optional[_Union[ExternalFunctionVersionKey, _Mapping]] = ...,
        include_scaling_group: bool = ...,
        include_active_schedule: bool = ...,
        visibility: _Optional[_Iterable[_Union[ExternalFunctionVisibility, str]]] = ...,
        include_deleted: bool = ...,
    ) -> None: ...

class GetExternalFunctionVersionResponse(_message.Message):
    __slots__ = ("external_function_version", "scaling_group", "active_schedule")
    EXTERNAL_FUNCTION_VERSION_FIELD_NUMBER: _ClassVar[int]
    SCALING_GROUP_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_SCHEDULE_FIELD_NUMBER: _ClassVar[int]
    external_function_version: ExternalFunctionVersion
    scaling_group: _service_pb2.ScalingGroupResponse
    active_schedule: ActiveSchedule
    def __init__(
        self,
        external_function_version: _Optional[
            _Union[ExternalFunctionVersion, _Mapping]
        ] = ...,
        scaling_group: _Optional[
            _Union[_service_pb2.ScalingGroupResponse, _Mapping]
        ] = ...,
        active_schedule: _Optional[_Union[ActiveSchedule, _Mapping]] = ...,
    ) -> None: ...

class ExternalFunctionVersionSourceFile(_message.Message):
    __slots__ = ("path", "size")
    PATH_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    path: str
    size: int
    def __init__(
        self, path: _Optional[str] = ..., size: _Optional[int] = ...
    ) -> None: ...

class GetExternalFunctionVersionSourceRequest(_message.Message):
    __slots__ = ("function_version_id",)
    FUNCTION_VERSION_ID_FIELD_NUMBER: _ClassVar[int]
    function_version_id: str
    def __init__(self, function_version_id: _Optional[str] = ...) -> None: ...

class GetExternalFunctionVersionSourceResponse(_message.Message):
    __slots__ = ("volume_name", "volume_version_id", "primary_path", "source_files")
    VOLUME_NAME_FIELD_NUMBER: _ClassVar[int]
    VOLUME_VERSION_ID_FIELD_NUMBER: _ClassVar[int]
    PRIMARY_PATH_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FILES_FIELD_NUMBER: _ClassVar[int]
    volume_name: str
    volume_version_id: int
    primary_path: str
    source_files: _containers.RepeatedCompositeFieldContainer[
        ExternalFunctionVersionSourceFile
    ]
    def __init__(
        self,
        volume_name: _Optional[str] = ...,
        volume_version_id: _Optional[int] = ...,
        primary_path: _Optional[str] = ...,
        source_files: _Optional[
            _Iterable[_Union[ExternalFunctionVersionSourceFile, _Mapping]]
        ] = ...,
    ) -> None: ...

class ActiveSchedule(_message.Message):
    __slots__ = ("cron",)
    CRON_FIELD_NUMBER: _ClassVar[int]
    cron: str
    def __init__(self, cron: _Optional[str] = ...) -> None: ...

class ListExternalFunctionVersionsRequest(_message.Message):
    __slots__ = (
        "cursor",
        "limit",
        "include_scaling_group",
        "include_deleted",
        "function_name",
        "filters",
        "ids",
        "read_mask",
    )
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_SCALING_GROUP_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_DELETED_FIELD_NUMBER: _ClassVar[int]
    FUNCTION_NAME_FIELD_NUMBER: _ClassVar[int]
    FILTERS_FIELD_NUMBER: _ClassVar[int]
    IDS_FIELD_NUMBER: _ClassVar[int]
    READ_MASK_FIELD_NUMBER: _ClassVar[int]
    cursor: str
    limit: int
    include_scaling_group: bool
    include_deleted: bool
    function_name: str
    filters: ListExternalFunctionVersionsFilters
    ids: _containers.RepeatedScalarFieldContainer[str]
    read_mask: _field_mask_pb2.FieldMask
    def __init__(
        self,
        cursor: _Optional[str] = ...,
        limit: _Optional[int] = ...,
        include_scaling_group: bool = ...,
        include_deleted: bool = ...,
        function_name: _Optional[str] = ...,
        filters: _Optional[_Union[ListExternalFunctionVersionsFilters, _Mapping]] = ...,
        ids: _Optional[_Iterable[str]] = ...,
        read_mask: _Optional[_Union[_field_mask_pb2.FieldMask, _Mapping]] = ...,
    ) -> None: ...

class ListExternalFunctionVersionsFilters(_message.Message):
    __slots__ = ("visibility",)
    VISIBILITY_FIELD_NUMBER: _ClassVar[int]
    visibility: _containers.RepeatedScalarFieldContainer[ExternalFunctionVisibility]
    def __init__(
        self,
        visibility: _Optional[_Iterable[_Union[ExternalFunctionVisibility, str]]] = ...,
    ) -> None: ...

class ListExternalFunctionVersionsEntry(_message.Message):
    __slots__ = ("external_function_version", "scaling_group")
    EXTERNAL_FUNCTION_VERSION_FIELD_NUMBER: _ClassVar[int]
    SCALING_GROUP_FIELD_NUMBER: _ClassVar[int]
    external_function_version: ExternalFunctionVersion
    scaling_group: _service_pb2.ScalingGroupResponse
    def __init__(
        self,
        external_function_version: _Optional[
            _Union[ExternalFunctionVersion, _Mapping]
        ] = ...,
        scaling_group: _Optional[
            _Union[_service_pb2.ScalingGroupResponse, _Mapping]
        ] = ...,
    ) -> None: ...

class ListExternalFunctionVersionsResponse(_message.Message):
    __slots__ = ("entries", "next_cursor")
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[
        ListExternalFunctionVersionsEntry
    ]
    next_cursor: str
    def __init__(
        self,
        entries: _Optional[
            _Iterable[_Union[ListExternalFunctionVersionsEntry, _Mapping]]
        ] = ...,
        next_cursor: _Optional[str] = ...,
    ) -> None: ...

class DeleteExternalFunctionVersionRequest(_message.Message):
    __slots__ = ("id", "key")
    ID_FIELD_NUMBER: _ClassVar[int]
    KEY_FIELD_NUMBER: _ClassVar[int]
    id: str
    key: ExternalFunctionVersionKey
    def __init__(
        self,
        id: _Optional[str] = ...,
        key: _Optional[_Union[ExternalFunctionVersionKey, _Mapping]] = ...,
    ) -> None: ...

class DeleteExternalFunctionVersionResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class DeleteExternalFunctionRequest(_message.Message):
    __slots__ = ("function_name",)
    FUNCTION_NAME_FIELD_NUMBER: _ClassVar[int]
    function_name: str
    def __init__(self, function_name: _Optional[str] = ...) -> None: ...

class DeleteExternalFunctionResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ExternalFunctionSummary(_message.Message):
    __slots__ = (
        "name",
        "latest_version",
        "latest_scaling_group_name",
        "latest_updated_at",
        "config",
        "scaling_group",
        "active_schedule",
        "visibility",
        "created_at",
    )
    NAME_FIELD_NUMBER: _ClassVar[int]
    LATEST_VERSION_FIELD_NUMBER: _ClassVar[int]
    LATEST_SCALING_GROUP_NAME_FIELD_NUMBER: _ClassVar[int]
    LATEST_UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    SCALING_GROUP_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_SCHEDULE_FIELD_NUMBER: _ClassVar[int]
    VISIBILITY_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    name: str
    latest_version: int
    latest_scaling_group_name: str
    latest_updated_at: _timestamp_pb2.Timestamp
    config: FunctionConfig
    scaling_group: _service_pb2.ScalingGroupResponse
    active_schedule: ActiveSchedule
    visibility: _service_pb2.ScalingGroupVisibility
    created_at: _timestamp_pb2.Timestamp
    def __init__(
        self,
        name: _Optional[str] = ...,
        latest_version: _Optional[int] = ...,
        latest_scaling_group_name: _Optional[str] = ...,
        latest_updated_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        config: _Optional[_Union[FunctionConfig, _Mapping]] = ...,
        scaling_group: _Optional[
            _Union[_service_pb2.ScalingGroupResponse, _Mapping]
        ] = ...,
        active_schedule: _Optional[_Union[ActiveSchedule, _Mapping]] = ...,
        visibility: _Optional[_Union[_service_pb2.ScalingGroupVisibility, str]] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
    ) -> None: ...

class ListExternalFunctionsRequest(_message.Message):
    __slots__ = (
        "cursor",
        "limit",
        "include_scaling_group",
        "include_active_schedule",
        "include_deleted",
        "filters",
        "search",
        "sort_column",
        "sort_order",
    )
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_SCALING_GROUP_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_ACTIVE_SCHEDULE_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_DELETED_FIELD_NUMBER: _ClassVar[int]
    FILTERS_FIELD_NUMBER: _ClassVar[int]
    SEARCH_FIELD_NUMBER: _ClassVar[int]
    SORT_COLUMN_FIELD_NUMBER: _ClassVar[int]
    SORT_ORDER_FIELD_NUMBER: _ClassVar[int]
    cursor: str
    limit: int
    include_scaling_group: bool
    include_active_schedule: bool
    include_deleted: bool
    filters: ListExternalFunctionsFilters
    search: str
    sort_column: ExternalFunctionSortColumn
    sort_order: ExternalFunctionSortOrder
    def __init__(
        self,
        cursor: _Optional[str] = ...,
        limit: _Optional[int] = ...,
        include_scaling_group: bool = ...,
        include_active_schedule: bool = ...,
        include_deleted: bool = ...,
        filters: _Optional[_Union[ListExternalFunctionsFilters, _Mapping]] = ...,
        search: _Optional[str] = ...,
        sort_column: _Optional[_Union[ExternalFunctionSortColumn, str]] = ...,
        sort_order: _Optional[_Union[ExternalFunctionSortOrder, str]] = ...,
    ) -> None: ...

class ListExternalFunctionsFilters(_message.Message):
    __slots__ = ("visibility", "statuses")
    VISIBILITY_FIELD_NUMBER: _ClassVar[int]
    STATUSES_FIELD_NUMBER: _ClassVar[int]
    visibility: _containers.RepeatedScalarFieldContainer[ExternalFunctionVisibility]
    statuses: _containers.RepeatedScalarFieldContainer[str]
    def __init__(
        self,
        visibility: _Optional[_Iterable[_Union[ExternalFunctionVisibility, str]]] = ...,
        statuses: _Optional[_Iterable[str]] = ...,
    ) -> None: ...

class ListExternalFunctionsResponse(_message.Message):
    __slots__ = ("functions", "next_cursor")
    FUNCTIONS_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    functions: _containers.RepeatedCompositeFieldContainer[ExternalFunctionSummary]
    next_cursor: str
    def __init__(
        self,
        functions: _Optional[
            _Iterable[_Union[ExternalFunctionSummary, _Mapping]]
        ] = ...,
        next_cursor: _Optional[str] = ...,
    ) -> None: ...

class ListExternalFunctionScheduledRunsRequest(_message.Message):
    __slots__ = ("function_name", "cursor", "limit")
    FUNCTION_NAME_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    function_name: str
    cursor: str
    limit: int
    def __init__(
        self,
        function_name: _Optional[str] = ...,
        cursor: _Optional[str] = ...,
        limit: _Optional[int] = ...,
    ) -> None: ...

class ExternalFunctionScheduledRun(_message.Message):
    __slots__ = ("id", "status", "created_at", "metadata")
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
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    id: int
    status: ExternalFunctionScheduledRunStatus
    created_at: _timestamp_pb2.Timestamp
    metadata: _containers.MessageMap[str, _struct_pb2.Value]
    def __init__(
        self,
        id: _Optional[int] = ...,
        status: _Optional[_Union[ExternalFunctionScheduledRunStatus, str]] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        metadata: _Optional[_Mapping[str, _struct_pb2.Value]] = ...,
    ) -> None: ...

class ListExternalFunctionScheduledRunsResponse(_message.Message):
    __slots__ = ("runs", "next_cursor")
    RUNS_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    runs: _containers.RepeatedCompositeFieldContainer[ExternalFunctionScheduledRun]
    next_cursor: str
    def __init__(
        self,
        runs: _Optional[
            _Iterable[_Union[ExternalFunctionScheduledRun, _Mapping]]
        ] = ...,
        next_cursor: _Optional[str] = ...,
    ) -> None: ...

class CallExternalFunctionRequest(_message.Message):
    __slots__ = ("function", "remote_call_request", "enqueue_remote_call_request")
    FUNCTION_FIELD_NUMBER: _ClassVar[int]
    REMOTE_CALL_REQUEST_FIELD_NUMBER: _ClassVar[int]
    ENQUEUE_REMOTE_CALL_REQUEST_FIELD_NUMBER: _ClassVar[int]
    function: ExternalFunctionVersionKey
    remote_call_request: _remote_python_call_pb2.CallFunctionRequest
    enqueue_remote_call_request: _remote_python_call_pb2.EnqueueRemoteCallRequest
    def __init__(
        self,
        function: _Optional[_Union[ExternalFunctionVersionKey, _Mapping]] = ...,
        remote_call_request: _Optional[
            _Union[_remote_python_call_pb2.CallFunctionRequest, _Mapping]
        ] = ...,
        enqueue_remote_call_request: _Optional[
            _Union[_remote_python_call_pb2.EnqueueRemoteCallRequest, _Mapping]
        ] = ...,
    ) -> None: ...

class CallExternalFunctionResponse(_message.Message):
    __slots__ = ("remote_call_response", "enqueue_remote_call_response")
    REMOTE_CALL_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    ENQUEUE_REMOTE_CALL_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    remote_call_response: _remote_python_call_pb2.CallFunctionResponse
    enqueue_remote_call_response: _remote_python_call_pb2.EnqueueRemoteCallResponse
    def __init__(
        self,
        remote_call_response: _Optional[
            _Union[_remote_python_call_pb2.CallFunctionResponse, _Mapping]
        ] = ...,
        enqueue_remote_call_response: _Optional[
            _Union[_remote_python_call_pb2.EnqueueRemoteCallResponse, _Mapping]
        ] = ...,
    ) -> None: ...
