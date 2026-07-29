from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
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

class ArtifactProducer(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ARTIFACT_PRODUCER_UNSPECIFIED: _ClassVar[ArtifactProducer]
    ARTIFACT_PRODUCER_USER: _ClassVar[ArtifactProducer]
    ARTIFACT_PRODUCER_SYSTEM: _ClassVar[ArtifactProducer]

class ArtifactKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ARTIFACT_KIND_UNSPECIFIED: _ClassVar[ArtifactKind]
    ARTIFACT_KIND_FILE: _ClassVar[ArtifactKind]
    ARTIFACT_KIND_CHART: _ClassVar[ArtifactKind]
    ARTIFACT_KIND_HTML: _ClassVar[ArtifactKind]
    ARTIFACT_KIND_IMAGE: _ClassVar[ArtifactKind]

class ArtifactStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ARTIFACT_STATUS_UNSPECIFIED: _ClassVar[ArtifactStatus]
    ARTIFACT_STATUS_PENDING_UPLOAD: _ClassVar[ArtifactStatus]
    ARTIFACT_STATUS_UPLOADED: _ClassVar[ArtifactStatus]
    ARTIFACT_STATUS_FAILED: _ClassVar[ArtifactStatus]

ARTIFACT_PRODUCER_UNSPECIFIED: ArtifactProducer
ARTIFACT_PRODUCER_USER: ArtifactProducer
ARTIFACT_PRODUCER_SYSTEM: ArtifactProducer
ARTIFACT_KIND_UNSPECIFIED: ArtifactKind
ARTIFACT_KIND_FILE: ArtifactKind
ARTIFACT_KIND_CHART: ArtifactKind
ARTIFACT_KIND_HTML: ArtifactKind
ARTIFACT_KIND_IMAGE: ArtifactKind
ARTIFACT_STATUS_UNSPECIFIED: ArtifactStatus
ARTIFACT_STATUS_PENDING_UPLOAD: ArtifactStatus
ARTIFACT_STATUS_UPLOADED: ArtifactStatus
ARTIFACT_STATUS_FAILED: ArtifactStatus

class ArtifactOrigin(_message.Message):
    __slots__ = ("sandbox_exec",)
    SANDBOX_EXEC_FIELD_NUMBER: _ClassVar[int]
    sandbox_exec: SandboxExecOrigin
    def __init__(
        self, sandbox_exec: _Optional[_Union[SandboxExecOrigin, _Mapping]] = ...
    ) -> None: ...

class SandboxExecOrigin(_message.Message):
    __slots__ = ("sandbox_id", "execution_id")
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    sandbox_id: str
    execution_id: str
    def __init__(
        self, sandbox_id: _Optional[str] = ..., execution_id: _Optional[str] = ...
    ) -> None: ...

class ChartDetails(_message.Message):
    __slots__ = ("vega_lite", "tabular", "plotly", "variable_name")
    VEGA_LITE_FIELD_NUMBER: _ClassVar[int]
    TABULAR_FIELD_NUMBER: _ClassVar[int]
    PLOTLY_FIELD_NUMBER: _ClassVar[int]
    VARIABLE_NAME_FIELD_NUMBER: _ClassVar[int]
    vega_lite: VegaLiteChartDetails
    tabular: TabularChartDetails
    plotly: PlotlyChartDetails
    variable_name: str
    def __init__(
        self,
        vega_lite: _Optional[_Union[VegaLiteChartDetails, _Mapping]] = ...,
        tabular: _Optional[_Union[TabularChartDetails, _Mapping]] = ...,
        plotly: _Optional[_Union[PlotlyChartDetails, _Mapping]] = ...,
        variable_name: _Optional[str] = ...,
    ) -> None: ...

class VegaLiteChartDetails(_message.Message):
    __slots__ = ("source", "transforms_materialized")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    TRANSFORMS_MATERIALIZED_FIELD_NUMBER: _ClassVar[int]
    source: str
    transforms_materialized: bool
    def __init__(
        self, source: _Optional[str] = ..., transforms_materialized: bool = ...
    ) -> None: ...

class TabularChartDetails(_message.Message):
    __slots__ = ("source", "schema_version")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    source: str
    schema_version: int
    def __init__(
        self, source: _Optional[str] = ..., schema_version: _Optional[int] = ...
    ) -> None: ...

class PlotlyChartDetails(_message.Message):
    __slots__ = ("plotly_version",)
    PLOTLY_VERSION_FIELD_NUMBER: _ClassVar[int]
    plotly_version: str
    def __init__(self, plotly_version: _Optional[str] = ...) -> None: ...

class HtmlDetails(_message.Message):
    __slots__ = ("entrypoint",)
    ENTRYPOINT_FIELD_NUMBER: _ClassVar[int]
    entrypoint: str
    def __init__(self, entrypoint: _Optional[str] = ...) -> None: ...

class ImageDetails(_message.Message):
    __slots__ = ("width", "height")
    WIDTH_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    width: int
    height: int
    def __init__(
        self, width: _Optional[int] = ..., height: _Optional[int] = ...
    ) -> None: ...

class ArtifactBlobRef(_message.Message):
    __slots__ = ("byte_size", "content_sha256")
    BYTE_SIZE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_SHA256_FIELD_NUMBER: _ClassVar[int]
    byte_size: int
    content_sha256: str
    def __init__(
        self, byte_size: _Optional[int] = ..., content_sha256: _Optional[str] = ...
    ) -> None: ...

class ArtifactInlineContent(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...

class Artifact(_message.Message):
    __slots__ = (
        "id",
        "environment_id",
        "name",
        "kind",
        "producer",
        "origin",
        "content_type",
        "status",
        "inline",
        "blob",
        "chart",
        "html",
        "image",
        "metadata",
        "created_at",
    )
    ID_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    PRODUCER_FIELD_NUMBER: _ClassVar[int]
    ORIGIN_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    INLINE_FIELD_NUMBER: _ClassVar[int]
    BLOB_FIELD_NUMBER: _ClassVar[int]
    CHART_FIELD_NUMBER: _ClassVar[int]
    HTML_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    id: str
    environment_id: str
    name: str
    kind: ArtifactKind
    producer: ArtifactProducer
    origin: ArtifactOrigin
    content_type: str
    status: ArtifactStatus
    inline: ArtifactInlineContent
    blob: ArtifactBlobRef
    chart: ChartDetails
    html: HtmlDetails
    image: ImageDetails
    metadata: _struct_pb2.Struct
    created_at: _timestamp_pb2.Timestamp
    def __init__(
        self,
        id: _Optional[str] = ...,
        environment_id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        kind: _Optional[_Union[ArtifactKind, str]] = ...,
        producer: _Optional[_Union[ArtifactProducer, str]] = ...,
        origin: _Optional[_Union[ArtifactOrigin, _Mapping]] = ...,
        content_type: _Optional[str] = ...,
        status: _Optional[_Union[ArtifactStatus, str]] = ...,
        inline: _Optional[_Union[ArtifactInlineContent, _Mapping]] = ...,
        blob: _Optional[_Union[ArtifactBlobRef, _Mapping]] = ...,
        chart: _Optional[_Union[ChartDetails, _Mapping]] = ...,
        html: _Optional[_Union[HtmlDetails, _Mapping]] = ...,
        image: _Optional[_Union[ImageDetails, _Mapping]] = ...,
        metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
    ) -> None: ...

class CreateArtifactRequest(_message.Message):
    __slots__ = ("artifact",)
    ARTIFACT_FIELD_NUMBER: _ClassVar[int]
    artifact: Artifact
    def __init__(
        self, artifact: _Optional[_Union[Artifact, _Mapping]] = ...
    ) -> None: ...

class CreateArtifactResponse(_message.Message):
    __slots__ = ("artifact",)
    ARTIFACT_FIELD_NUMBER: _ClassVar[int]
    artifact: Artifact
    def __init__(
        self, artifact: _Optional[_Union[Artifact, _Mapping]] = ...
    ) -> None: ...

class CreateArtifactUploadRequest(_message.Message):
    __slots__ = ("artifact", "expected_byte_size", "expected_content_sha256")
    ARTIFACT_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_BYTE_SIZE_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_CONTENT_SHA256_FIELD_NUMBER: _ClassVar[int]
    artifact: Artifact
    expected_byte_size: int
    expected_content_sha256: str
    def __init__(
        self,
        artifact: _Optional[_Union[Artifact, _Mapping]] = ...,
        expected_byte_size: _Optional[int] = ...,
        expected_content_sha256: _Optional[str] = ...,
    ) -> None: ...

class ArtifactUploadHeader(_message.Message):
    __slots__ = ("name", "value")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    name: str
    value: str
    def __init__(
        self, name: _Optional[str] = ..., value: _Optional[str] = ...
    ) -> None: ...

class CreateArtifactUploadResponse(_message.Message):
    __slots__ = ("artifact", "upload_url", "method", "headers", "expires_at")
    ARTIFACT_FIELD_NUMBER: _ClassVar[int]
    UPLOAD_URL_FIELD_NUMBER: _ClassVar[int]
    METHOD_FIELD_NUMBER: _ClassVar[int]
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    artifact: Artifact
    upload_url: str
    method: str
    headers: _containers.RepeatedCompositeFieldContainer[ArtifactUploadHeader]
    expires_at: _timestamp_pb2.Timestamp
    def __init__(
        self,
        artifact: _Optional[_Union[Artifact, _Mapping]] = ...,
        upload_url: _Optional[str] = ...,
        method: _Optional[str] = ...,
        headers: _Optional[_Iterable[_Union[ArtifactUploadHeader, _Mapping]]] = ...,
        expires_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
    ) -> None: ...

class FinalizeArtifactUploadRequest(_message.Message):
    __slots__ = ("artifact_id", "byte_size", "content_sha256")
    ARTIFACT_ID_FIELD_NUMBER: _ClassVar[int]
    BYTE_SIZE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_SHA256_FIELD_NUMBER: _ClassVar[int]
    artifact_id: str
    byte_size: int
    content_sha256: str
    def __init__(
        self,
        artifact_id: _Optional[str] = ...,
        byte_size: _Optional[int] = ...,
        content_sha256: _Optional[str] = ...,
    ) -> None: ...

class FinalizeArtifactUploadResponse(_message.Message):
    __slots__ = ("artifact",)
    ARTIFACT_FIELD_NUMBER: _ClassVar[int]
    artifact: Artifact
    def __init__(
        self, artifact: _Optional[_Union[Artifact, _Mapping]] = ...
    ) -> None: ...

class GetArtifactRequest(_message.Message):
    __slots__ = ("artifact_id",)
    ARTIFACT_ID_FIELD_NUMBER: _ClassVar[int]
    artifact_id: str
    def __init__(self, artifact_id: _Optional[str] = ...) -> None: ...

class GetArtifactResponse(_message.Message):
    __slots__ = ("artifact",)
    ARTIFACT_FIELD_NUMBER: _ClassVar[int]
    artifact: Artifact
    def __init__(
        self, artifact: _Optional[_Union[Artifact, _Mapping]] = ...
    ) -> None: ...

class ListArtifactsRequest(_message.Message):
    __slots__ = ("kinds", "producers", "sandbox_id", "cursor", "limit")
    KINDS_FIELD_NUMBER: _ClassVar[int]
    PRODUCERS_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    kinds: _containers.RepeatedScalarFieldContainer[ArtifactKind]
    producers: _containers.RepeatedScalarFieldContainer[ArtifactProducer]
    sandbox_id: str
    cursor: str
    limit: int
    def __init__(
        self,
        kinds: _Optional[_Iterable[_Union[ArtifactKind, str]]] = ...,
        producers: _Optional[_Iterable[_Union[ArtifactProducer, str]]] = ...,
        sandbox_id: _Optional[str] = ...,
        cursor: _Optional[str] = ...,
        limit: _Optional[int] = ...,
    ) -> None: ...

class ListArtifactsResponse(_message.Message):
    __slots__ = ("artifacts", "next_cursor")
    ARTIFACTS_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    artifacts: _containers.RepeatedCompositeFieldContainer[Artifact]
    next_cursor: str
    def __init__(
        self,
        artifacts: _Optional[_Iterable[_Union[Artifact, _Mapping]]] = ...,
        next_cursor: _Optional[str] = ...,
    ) -> None: ...

class DownloadArtifactRequest(_message.Message):
    __slots__ = ("artifact_id",)
    ARTIFACT_ID_FIELD_NUMBER: _ClassVar[int]
    artifact_id: str
    def __init__(self, artifact_id: _Optional[str] = ...) -> None: ...

class DownloadArtifactResponse(_message.Message):
    __slots__ = ("data", "content_type", "file_name")
    DATA_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    FILE_NAME_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    content_type: str
    file_name: str
    def __init__(
        self,
        data: _Optional[bytes] = ...,
        content_type: _Optional[str] = ...,
        file_name: _Optional[str] = ...,
    ) -> None: ...

class DeleteArtifactRequest(_message.Message):
    __slots__ = ("artifact_id",)
    ARTIFACT_ID_FIELD_NUMBER: _ClassVar[int]
    artifact_id: str
    def __init__(self, artifact_id: _Optional[str] = ...) -> None: ...

class DeleteArtifactResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
