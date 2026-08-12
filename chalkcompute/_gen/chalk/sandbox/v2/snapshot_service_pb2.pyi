from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
from chalkcompute._gen.chalk.container.v1 import service_pb2 as _service_pb2
from chalkcompute._gen.chalk.flags.v1 import flags_pb2 as _flags_pb2
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

class SandboxSnapshotStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SANDBOX_SNAPSHOT_STATUS_UNSPECIFIED: _ClassVar[SandboxSnapshotStatus]
    SANDBOX_SNAPSHOT_STATUS_PENDING: _ClassVar[SandboxSnapshotStatus]
    SANDBOX_SNAPSHOT_STATUS_IN_PROGRESS: _ClassVar[SandboxSnapshotStatus]
    SANDBOX_SNAPSHOT_STATUS_COMPLETED: _ClassVar[SandboxSnapshotStatus]
    SANDBOX_SNAPSHOT_STATUS_FAILED: _ClassVar[SandboxSnapshotStatus]

class SandboxSnapshotSortColumn(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SANDBOX_SNAPSHOT_SORT_COLUMN_UNSPECIFIED: _ClassVar[SandboxSnapshotSortColumn]
    SANDBOX_SNAPSHOT_SORT_COLUMN_CREATED_AT: _ClassVar[SandboxSnapshotSortColumn]

class SandboxSnapshotSortOrder(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SANDBOX_SNAPSHOT_SORT_ORDER_UNSPECIFIED: _ClassVar[SandboxSnapshotSortOrder]
    SANDBOX_SNAPSHOT_SORT_ORDER_DESC: _ClassVar[SandboxSnapshotSortOrder]
    SANDBOX_SNAPSHOT_SORT_ORDER_ASC: _ClassVar[SandboxSnapshotSortOrder]

SANDBOX_SNAPSHOT_STATUS_UNSPECIFIED: SandboxSnapshotStatus
SANDBOX_SNAPSHOT_STATUS_PENDING: SandboxSnapshotStatus
SANDBOX_SNAPSHOT_STATUS_IN_PROGRESS: SandboxSnapshotStatus
SANDBOX_SNAPSHOT_STATUS_COMPLETED: SandboxSnapshotStatus
SANDBOX_SNAPSHOT_STATUS_FAILED: SandboxSnapshotStatus
SANDBOX_SNAPSHOT_SORT_COLUMN_UNSPECIFIED: SandboxSnapshotSortColumn
SANDBOX_SNAPSHOT_SORT_COLUMN_CREATED_AT: SandboxSnapshotSortColumn
SANDBOX_SNAPSHOT_SORT_ORDER_UNSPECIFIED: SandboxSnapshotSortOrder
SANDBOX_SNAPSHOT_SORT_ORDER_DESC: SandboxSnapshotSortOrder
SANDBOX_SNAPSHOT_SORT_ORDER_ASC: SandboxSnapshotSortOrder

class GKEPodSnapshot(_message.Message):
    __slots__ = ("storage_bucket", "storage_path")
    STORAGE_BUCKET_FIELD_NUMBER: _ClassVar[int]
    STORAGE_PATH_FIELD_NUMBER: _ClassVar[int]
    storage_bucket: str
    storage_path: str
    def __init__(
        self, storage_bucket: _Optional[str] = ..., storage_path: _Optional[str] = ...
    ) -> None: ...

class SandboxSnapshotSpec(_message.Message):
    __slots__ = ("gke_pod_snapshot",)
    GKE_POD_SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    gke_pod_snapshot: GKEPodSnapshot
    def __init__(
        self, gke_pod_snapshot: _Optional[_Union[GKEPodSnapshot, _Mapping]] = ...
    ) -> None: ...

class SandboxSnapshot(_message.Message):
    __slots__ = (
        "id",
        "sandbox_id",
        "spec",
        "snapshot_spec",
        "status",
        "status_message",
        "created_at",
        "completed_at",
        "created_by",
    )
    ID_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    SPEC_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_SPEC_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_FIELD_NUMBER: _ClassVar[int]
    CREATED_BY_FIELD_NUMBER: _ClassVar[int]
    id: str
    sandbox_id: str
    spec: _service_pb2.ChalkContainerSpec
    snapshot_spec: SandboxSnapshotSpec
    status: SandboxSnapshotStatus
    status_message: str
    created_at: _timestamp_pb2.Timestamp
    completed_at: _timestamp_pb2.Timestamp
    created_by: str
    def __init__(
        self,
        id: _Optional[str] = ...,
        sandbox_id: _Optional[str] = ...,
        spec: _Optional[_Union[_service_pb2.ChalkContainerSpec, _Mapping]] = ...,
        snapshot_spec: _Optional[_Union[SandboxSnapshotSpec, _Mapping]] = ...,
        status: _Optional[_Union[SandboxSnapshotStatus, str]] = ...,
        status_message: _Optional[str] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        completed_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        created_by: _Optional[str] = ...,
    ) -> None: ...

class CreateSandboxSnapshotRequest(_message.Message):
    __slots__ = ("sandbox_id",)
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    sandbox_id: str
    def __init__(self, sandbox_id: _Optional[str] = ...) -> None: ...

class CreateSandboxSnapshotResponse(_message.Message):
    __slots__ = ("snapshot",)
    SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    snapshot: SandboxSnapshot
    def __init__(
        self, snapshot: _Optional[_Union[SandboxSnapshot, _Mapping]] = ...
    ) -> None: ...

class GetSandboxSnapshotRequest(_message.Message):
    __slots__ = ("snapshot_id",)
    SNAPSHOT_ID_FIELD_NUMBER: _ClassVar[int]
    snapshot_id: str
    def __init__(self, snapshot_id: _Optional[str] = ...) -> None: ...

class GetSandboxSnapshotResponse(_message.Message):
    __slots__ = ("snapshot",)
    SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    snapshot: SandboxSnapshot
    def __init__(
        self, snapshot: _Optional[_Union[SandboxSnapshot, _Mapping]] = ...
    ) -> None: ...

class ListSandboxSnapshotsFilters(_message.Message):
    __slots__ = ("sandbox_ids", "statuses")
    SANDBOX_IDS_FIELD_NUMBER: _ClassVar[int]
    STATUSES_FIELD_NUMBER: _ClassVar[int]
    sandbox_ids: _containers.RepeatedScalarFieldContainer[str]
    statuses: _containers.RepeatedScalarFieldContainer[str]
    def __init__(
        self,
        sandbox_ids: _Optional[_Iterable[str]] = ...,
        statuses: _Optional[_Iterable[str]] = ...,
    ) -> None: ...

class ListSandboxSnapshotsRequest(_message.Message):
    __slots__ = ("limit", "cursor", "search", "filters", "sort_column", "sort_order")
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    SEARCH_FIELD_NUMBER: _ClassVar[int]
    FILTERS_FIELD_NUMBER: _ClassVar[int]
    SORT_COLUMN_FIELD_NUMBER: _ClassVar[int]
    SORT_ORDER_FIELD_NUMBER: _ClassVar[int]
    limit: int
    cursor: str
    search: str
    filters: ListSandboxSnapshotsFilters
    sort_column: SandboxSnapshotSortColumn
    sort_order: SandboxSnapshotSortOrder
    def __init__(
        self,
        limit: _Optional[int] = ...,
        cursor: _Optional[str] = ...,
        search: _Optional[str] = ...,
        filters: _Optional[_Union[ListSandboxSnapshotsFilters, _Mapping]] = ...,
        sort_column: _Optional[_Union[SandboxSnapshotSortColumn, str]] = ...,
        sort_order: _Optional[_Union[SandboxSnapshotSortOrder, str]] = ...,
    ) -> None: ...

class ListSandboxSnapshotsResponse(_message.Message):
    __slots__ = ("snapshots", "next_cursor")
    SNAPSHOTS_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    snapshots: _containers.RepeatedCompositeFieldContainer[SandboxSnapshot]
    next_cursor: str
    def __init__(
        self,
        snapshots: _Optional[_Iterable[_Union[SandboxSnapshot, _Mapping]]] = ...,
        next_cursor: _Optional[str] = ...,
    ) -> None: ...

class DeleteSandboxSnapshotRequest(_message.Message):
    __slots__ = ("snapshot_id",)
    SNAPSHOT_ID_FIELD_NUMBER: _ClassVar[int]
    snapshot_id: str
    def __init__(self, snapshot_id: _Optional[str] = ...) -> None: ...

class DeleteSandboxSnapshotResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
