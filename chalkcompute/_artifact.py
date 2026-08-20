"""
Client for the Chalk artifact store (chalk.artifactstore.v1.ArtifactService).

Artifacts are durable, environment-scoped outputs: charts captured from
``Sandbox.execute_python``, files saved with the in-sandbox ``save_artifact``
helper, or content stored directly through :meth:`ArtifactClient.create`.

Usage:
    from chalkcompute import ArtifactClient, Sandbox

    sandbox = Sandbox(image="python:3.12-slim").run()
    execution = sandbox.execute_python(
        "import altair as alt..."
    )
    for chart in execution.charts:
        print(chart.name, chart.chart_encoding)

    client = ArtifactClient.from_env()
    for artifact in client.list(kinds=["chart"]):
        print(artifact.id, artifact.name)
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from connectrpc.protocol import ProtocolType
from protobuf import Oneof

from chalkcompute._connect import (
    ConnectClient,
    raise_for_rpc_error,
    rpc_address_from_target,
)
from chalkcompute._gen.chalk.artifactstore.v1 import service_pb as pb
from chalkcompute._gen.chalk.artifactstore.v1 import service_connect as pb_connect
from chalkcompute._http import SYNC_HTTP_CLIENT
from chalkcompute._rpc import RPC_READ_MAX_BYTES, rpc_interceptors
from chalkcompute._tracing import _instrumented

# Aliases defined before ArtifactClient, which has a ``.list()`` method that
# would otherwise shadow the builtin ``list`` in class-scope name resolution.
_GrpcMetadata = list[tuple[str, str]]
_StrList = list[str]

_KIND_TO_PROTO: dict[str, pb.ArtifactKind] = {
    "file": pb.ArtifactKind.FILE,
    "chart": pb.ArtifactKind.CHART,
    "html": pb.ArtifactKind.HTML,
    "image": pb.ArtifactKind.IMAGE,
}
_KIND_FROM_PROTO: dict[pb.ArtifactKind, str] = {v: k for k, v in _KIND_TO_PROTO.items()}

_PRODUCER_TO_PROTO: dict[str, pb.ArtifactProducer] = {
    "user": pb.ArtifactProducer.USER,
    "system": pb.ArtifactProducer.SYSTEM,
}
_PRODUCER_FROM_PROTO: dict[pb.ArtifactProducer, str] = {
    v: k for k, v in _PRODUCER_TO_PROTO.items()
}


class ArtifactError(Exception):
    """Base exception for artifact store errors."""

    pass


class ArtifactNotFoundError(ArtifactError):
    """Raised when an artifact is not found."""

    pass


@dataclass
class Artifact:
    """A stored artifact.

    Small artifacts carry their content inline in ``data``; larger ones are
    blob-backed and fetched on :meth:`download`.
    """

    id: str
    """Artifact identifier."""

    name: str
    """User-facing label."""

    kind: str
    """One of ``"file"``, ``"chart"``, ``"html"``, ``"image"``."""

    producer: str
    """``"user"`` (saved explicitly) or ``"system"`` (discovered by Chalk)."""

    content_type: str
    """MIME type of the content."""

    byte_size: int
    """Content size in bytes."""

    sandbox_id: str | None = None
    """The sandbox that produced this artifact, when it came from one."""

    execution_id: str | None = None
    """The ExecutePython execution that produced this artifact, if any."""

    chart_encoding: str | None = None
    """For charts: ``"vega_lite"``, ``"tabular"``, or ``"plotly"``."""

    chart_variable_name: str | None = None
    """For captured charts: the variable the figure was bound to."""

    data: bytes | None = None
    """Inline content, when the artifact is small enough to travel inline."""

    _client: ArtifactClient | None = field(default=None, repr=False, compare=False)

    def download(self) -> bytes:
        """Return the artifact's content bytes, fetching them if not inline."""
        if self.data is not None:
            return self.data
        if self._client is None:
            raise ArtifactError(
                "Artifact has no attached client. Fetch it via ArtifactClient to download content."
            )
        self.data = self._client.download(self.id)
        return self.data


def _artifact_from_proto(msg: pb.Artifact, client: ArtifactClient | None) -> Artifact:
    chart_encoding: str | None = None
    chart_variable_name: str | None = None
    if msg.details is not None and msg.details.field == "chart":
        chart = msg.details.value
        chart_variable_name = chart.variable_name or None
        if chart.chart is not None and chart.chart.field == "vega_lite":
            chart_encoding = "vega_lite"
        elif chart.chart is not None and chart.chart.field == "tabular":
            chart_encoding = "tabular"
        elif chart.chart is not None and chart.chart.field == "plotly":
            chart_encoding = "plotly"

    sandbox_id: str | None = None
    execution_id: str | None = None
    if (
        msg.origin is not None
        and msg.origin.origin is not None
        and msg.origin.origin.field == "sandbox_exec"
    ):
        sandbox_exec = msg.origin.origin.value
        sandbox_id = sandbox_exec.sandbox_id or None
        execution_id = sandbox_exec.execution_id or None

    data: bytes | None = None
    byte_size = 0
    if msg.content is not None and msg.content.field == "inline":
        data = msg.content.value.data
        byte_size = len(data)
    elif msg.content is not None and msg.content.field == "blob":
        byte_size = msg.content.value.byte_size

    return Artifact(
        id=msg.id,
        name=msg.name,
        kind=_KIND_FROM_PROTO.get(msg.kind, "file"),
        producer=_PRODUCER_FROM_PROTO.get(msg.producer, "user"),
        content_type=msg.content_type,
        byte_size=byte_size,
        sandbox_id=sandbox_id,
        execution_id=execution_id,
        chart_encoding=chart_encoding,
        chart_variable_name=chart_variable_name,
        data=data,
        _client=client,
    )


class ArtifactClient:
    """Client for the artifact store service."""

    def __init__(
        self,
        target: str = "localhost:50051",
        *,
        credentials: object | None = None,
        use_tls: bool | None = None,
        metadata: _GrpcMetadata | None = None,
        _connect: ConnectClient | None = None,
        _owns_connect: bool = False,
    ) -> None:
        self._target = target
        self._static_metadata = metadata
        self._connect = _connect
        self._owns_connect = _owns_connect
        if use_tls is None:
            use_tls = credentials is not None
        self._stub = pb_connect.ArtifactServiceClientSync(
            rpc_address_from_target(target, use_tls=use_tls),
            protocol=ProtocolType.GRPC,
            interceptors=rpc_interceptors(),
            http_client=SYNC_HTTP_CLIENT,
            read_max_bytes=RPC_READ_MAX_BYTES,
        )

    @classmethod
    def _from_connect(
        cls,
        connect: ConnectClient,
        *,
        _owns_connect: bool = False,
    ) -> ArtifactClient:
        """Internal: build an artifact client from shared Chalk authentication."""
        connect._ensure_auth()
        return cls(
            connect._api_server or "",
            use_tls=(connect._api_server or "").startswith("https://"),
            metadata=connect._rpc_metadata(),
            _connect=connect,
            _owns_connect=_owns_connect,
        )

    @classmethod
    def from_env(cls, *, chalk_client: Any | None = None) -> ArtifactClient:
        """Create an ``ArtifactClient`` from ambient Chalk credentials."""
        connect = ConnectClient(chalk_client=chalk_client)
        try:
            return cls._from_connect(connect, _owns_connect=True)
        except Exception:
            connect.close()
            raise

    @property
    def _metadata(self) -> _GrpcMetadata | None:
        if self._connect is not None:
            return self._connect._rpc_metadata()
        return self._static_metadata

    @_metadata.setter
    def _metadata(self, value: _GrpcMetadata | None) -> None:
        self._static_metadata = value

    def __enter__(self) -> ArtifactClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._stub.close()
        if self._owns_connect and self._connect is not None:
            self._connect.close()

    @_instrumented("chalkcompute.artifact.create")
    def create(
        self,
        data: bytes | str,
        *,
        name: str,
        kind: str = "file",
        content_type: str = "application/octet-stream",
    ) -> Artifact:
        """Store an artifact with inline content (up to a few MiB)."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        if kind not in _KIND_TO_PROTO:
            raise ArtifactError(
                f"unknown artifact kind {kind!r}; expected one of {sorted(_KIND_TO_PROTO)}"
            )
        spec = pb.Artifact(
            name=name,
            kind=_KIND_TO_PROTO[kind],
            producer=pb.ArtifactProducer.USER,
            content_type=content_type,
            content=Oneof("inline", pb.ArtifactInlineContent(data=data)),
        )
        try:
            resp = self._stub.create_artifact(
                pb.CreateArtifactRequest(artifact=spec),
                headers=dict(self._metadata or []),
            )
        except ConnectRpcError as e:
            _handle_artifact_rpc_error(e, name)
            raise
        if resp.artifact is None:
            raise ArtifactError("CreateArtifact returned no artifact")
        return _artifact_from_proto(resp.artifact, self)

    @_instrumented("chalkcompute.artifact.get")
    def get(self, artifact_id: str) -> Artifact:
        """Fetch a single artifact by id."""
        try:
            resp = self._stub.get_artifact(
                pb.GetArtifactRequest(artifact_id=artifact_id),
                headers=dict(self._metadata or []),
            )
        except ConnectRpcError as e:
            _handle_artifact_rpc_error(e, artifact_id)
            raise
        if resp.artifact is None:
            raise ArtifactError(f"GetArtifact returned no artifact: {artifact_id}")
        return _artifact_from_proto(resp.artifact, self)

    @_instrumented("chalkcompute.artifact.list")
    def list(
        self,
        *,
        kinds: _StrList | None = None,
        producers: _StrList | None = None,
        sandbox_id: str | None = None,
        limit: int | None = None,
    ) -> Iterator[Artifact]:
        """Iterate artifacts, newest first, paging transparently."""
        cursor: str | None = None
        remaining = limit
        while True:
            req = pb.ListArtifactsRequest(
                kinds=[_KIND_TO_PROTO[k] for k in kinds or []],
                producers=[_PRODUCER_TO_PROTO[p] for p in producers or []],
            )
            if sandbox_id is not None:
                req.sandbox_id = sandbox_id
            if cursor is not None:
                req.cursor = cursor
            if remaining is not None:
                req.limit = min(remaining, 100)
            try:
                resp = self._stub.list_artifacts(
                    req, headers=dict(self._metadata or [])
                )
            except ConnectRpcError as e:
                _handle_artifact_rpc_error(e, "list")
                raise
            for msg in resp.artifacts:
                yield _artifact_from_proto(msg, self)
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return
            if not resp.has_field("next_cursor") or not resp.next_cursor:
                return
            cursor = resp.next_cursor

    @_instrumented("chalkcompute.artifact.download")
    def download(self, artifact_id: str) -> bytes:
        """Download an artifact's content bytes."""
        chunks: list[bytes] = []
        try:
            for frame in self._stub.download_artifact(
                pb.DownloadArtifactRequest(artifact_id=artifact_id),
                headers=dict(self._metadata or []),
            ):
                chunks.append(frame.data)
        except ConnectRpcError as e:
            _handle_artifact_rpc_error(e, artifact_id)
            raise
        return b"".join(chunks)

    @_instrumented("chalkcompute.artifact.delete")
    def delete(self, artifact_id: str) -> None:
        """Delete an artifact and its stored content."""
        try:
            self._stub.delete_artifact(
                pb.DeleteArtifactRequest(artifact_id=artifact_id),
                headers=dict(self._metadata or []),
            )
        except ConnectRpcError as e:
            _handle_artifact_rpc_error(e, artifact_id)
            raise


def _handle_artifact_rpc_error(e: ConnectRpcError, subject: str) -> None:
    """Translate mapped RPC status codes to typed artifact errors.

    Unmapped codes return so the caller re-raises the original transport error,
    matching the sandbox/volume client convention.
    """
    raise_for_rpc_error(
        e,
        {
            Code.NOT_FOUND: (
                ArtifactNotFoundError,
                f"Artifact not found: {subject}",
            ),
        },
    )
