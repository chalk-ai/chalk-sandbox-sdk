from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
from chalkcompute._gen.chalk.container.v1 import service_pb2 as _service_pb2
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

class SandboxStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SANDBOX_STATUS_UNSPECIFIED: _ClassVar[SandboxStatus]
    SANDBOX_STATUS_PENDING: _ClassVar[SandboxStatus]
    SANDBOX_STATUS_RUNNING: _ClassVar[SandboxStatus]
    SANDBOX_STATUS_SUCCEEDED: _ClassVar[SandboxStatus]
    SANDBOX_STATUS_FAILED: _ClassVar[SandboxStatus]
    SANDBOX_STATUS_TERMINATED: _ClassVar[SandboxStatus]
    SANDBOX_STATUS_ERROR: _ClassVar[SandboxStatus]
    SANDBOX_STATUS_UNKNOWN: _ClassVar[SandboxStatus]

class SandboxSortColumn(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SANDBOX_SORT_COLUMN_UNSPECIFIED: _ClassVar[SandboxSortColumn]
    SANDBOX_SORT_COLUMN_CREATED_AT: _ClassVar[SandboxSortColumn]

class SandboxSortOrder(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SANDBOX_SORT_ORDER_UNSPECIFIED: _ClassVar[SandboxSortOrder]
    SANDBOX_SORT_ORDER_DESC: _ClassVar[SandboxSortOrder]
    SANDBOX_SORT_ORDER_ASC: _ClassVar[SandboxSortOrder]

SANDBOX_STATUS_UNSPECIFIED: SandboxStatus
SANDBOX_STATUS_PENDING: SandboxStatus
SANDBOX_STATUS_RUNNING: SandboxStatus
SANDBOX_STATUS_SUCCEEDED: SandboxStatus
SANDBOX_STATUS_FAILED: SandboxStatus
SANDBOX_STATUS_TERMINATED: SandboxStatus
SANDBOX_STATUS_ERROR: SandboxStatus
SANDBOX_STATUS_UNKNOWN: SandboxStatus
SANDBOX_SORT_COLUMN_UNSPECIFIED: SandboxSortColumn
SANDBOX_SORT_COLUMN_CREATED_AT: SandboxSortColumn
SANDBOX_SORT_ORDER_UNSPECIFIED: SandboxSortOrder
SANDBOX_SORT_ORDER_DESC: SandboxSortOrder
SANDBOX_SORT_ORDER_ASC: SandboxSortOrder

class SandboxInfo(_message.Message):
    __slots__ = (
        "id",
        "name",
        "status",
        "status_message",
        "spec",
        "created_at",
        "finished_at",
        "web_url",
    )
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    SPEC_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    FINISHED_AT_FIELD_NUMBER: _ClassVar[int]
    WEB_URL_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    status: SandboxStatus
    status_message: str
    spec: _service_pb2.ChalkContainerSpec
    created_at: _timestamp_pb2.Timestamp
    finished_at: _timestamp_pb2.Timestamp
    web_url: str
    def __init__(
        self,
        id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        status: _Optional[_Union[SandboxStatus, str]] = ...,
        status_message: _Optional[str] = ...,
        spec: _Optional[_Union[_service_pb2.ChalkContainerSpec, _Mapping]] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        finished_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        web_url: _Optional[str] = ...,
    ) -> None: ...

class CreateSandboxRequest(_message.Message):
    __slots__ = ("spec", "snapshot_id")
    SPEC_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_ID_FIELD_NUMBER: _ClassVar[int]
    spec: _service_pb2.ChalkContainerSpec
    snapshot_id: str
    def __init__(
        self,
        spec: _Optional[_Union[_service_pb2.ChalkContainerSpec, _Mapping]] = ...,
        snapshot_id: _Optional[str] = ...,
    ) -> None: ...

class CreateSandboxResponse(_message.Message):
    __slots__ = ("sandbox",)
    SANDBOX_FIELD_NUMBER: _ClassVar[int]
    sandbox: SandboxInfo
    def __init__(
        self, sandbox: _Optional[_Union[SandboxInfo, _Mapping]] = ...
    ) -> None: ...

class GetSandboxRequest(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class GetSandboxResponse(_message.Message):
    __slots__ = ("sandbox",)
    SANDBOX_FIELD_NUMBER: _ClassVar[int]
    sandbox: SandboxInfo
    def __init__(
        self, sandbox: _Optional[_Union[SandboxInfo, _Mapping]] = ...
    ) -> None: ...

class ListSandboxesFilters(_message.Message):
    __slots__ = ("names", "statuses", "images")
    NAMES_FIELD_NUMBER: _ClassVar[int]
    STATUSES_FIELD_NUMBER: _ClassVar[int]
    IMAGES_FIELD_NUMBER: _ClassVar[int]
    names: _containers.RepeatedScalarFieldContainer[str]
    statuses: _containers.RepeatedScalarFieldContainer[SandboxStatus]
    images: _containers.RepeatedScalarFieldContainer[str]
    def __init__(
        self,
        names: _Optional[_Iterable[str]] = ...,
        statuses: _Optional[_Iterable[_Union[SandboxStatus, str]]] = ...,
        images: _Optional[_Iterable[str]] = ...,
    ) -> None: ...

class ListSandboxesRequest(_message.Message):
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
    filters: ListSandboxesFilters
    sort_column: SandboxSortColumn
    sort_order: SandboxSortOrder
    def __init__(
        self,
        limit: _Optional[int] = ...,
        cursor: _Optional[str] = ...,
        search: _Optional[str] = ...,
        filters: _Optional[_Union[ListSandboxesFilters, _Mapping]] = ...,
        sort_column: _Optional[_Union[SandboxSortColumn, str]] = ...,
        sort_order: _Optional[_Union[SandboxSortOrder, str]] = ...,
    ) -> None: ...

class ListSandboxesResponse(_message.Message):
    __slots__ = ("sandboxes", "next_cursor")
    SANDBOXES_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    sandboxes: _containers.RepeatedCompositeFieldContainer[SandboxInfo]
    next_cursor: str
    def __init__(
        self,
        sandboxes: _Optional[_Iterable[_Union[SandboxInfo, _Mapping]]] = ...,
        next_cursor: _Optional[str] = ...,
    ) -> None: ...

class SuspendSandboxRequest(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class SuspendSandboxResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ResumeSandboxRequest(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class ResumeSandboxResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class TerminateSandboxRequest(_message.Message):
    __slots__ = ("id", "grace_period")
    ID_FIELD_NUMBER: _ClassVar[int]
    GRACE_PERIOD_FIELD_NUMBER: _ClassVar[int]
    id: str
    grace_period: _duration_pb2.Duration
    def __init__(
        self,
        id: _Optional[str] = ...,
        grace_period: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ...,
    ) -> None: ...

class TerminateSandboxResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
