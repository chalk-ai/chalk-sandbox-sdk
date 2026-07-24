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

import grpc

from chalkcompute._connect import ConnectClient, grpc_target_from_url
from chalkcompute._gen.chalk.artifactstore.v1 import service_pb2 as pb
from chalkcompute._gen.chalk.artifactstore.v1 import service_pb2_grpc as pb_grpc
from chalkcompute._tracing import _instrumented

_GrpcMetadata = list[tuple[str, str]]

_KIND_TO_PROTO: dict[str, int] = {
    "file": pb.ARTIFACT_KIND_FILE,
    "chart": pb.ARTIFACT_KIND_CHART,
    "html": pb.ARTIFACT_KIND_HTML,
    "image": pb.ARTIFACT_KIND_IMAGE,
}
_KIND_FROM_PROTO: dict[int, str] = {v: k for k, v in _KIND_TO_PROTO.items()}

_PRODUCER_TO_PROTO: dict[str, int] = {
    "user": pb.ARTIFACT_PRODUCER_USER,
    "system": pb.ARTIFACT_PRODUCER_SYSTEM,
}
_PRODUCER_FROM_PROTO: dict[int, str] = {v: k for k, v in _PRODUCER_TO_PROTO.items()}


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
    if msg.HasField("chart"):
        chart_variable_name = msg.chart.variable_name or None
        which = msg.chart.WhichOneof("chart")
        if which == "vega_lite":
            chart_encoding = "vega_lite"
        elif which == "tabular":
            chart_encoding = "tabular"
        elif which == "plotly":
            chart_encoding = "plotly"

    sandbox_id: str | None = None
    execution_id: str | None = None
    if msg.origin.WhichOneof("origin") == "sandbox_exec":
        sandbox_id = msg.origin.sandbox_exec.sandbox_id or None
        execution_id = msg.origin.sandbox_exec.execution_id or None

    data: bytes | None = None
    byte_size = 0
    if msg.WhichOneof("content") == "inline":
        data = msg.inline.data
        byte_size = len(data)
    elif msg.WhichOneof("content") == "blob":
        byte_size = msg.blob.byte_size

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
    """Client for the artifact store gRPC service."""

    def __init__(
        self,
        target: str = "localhost:50051",
        *,
        credentials: grpc.ChannelCredentials | None = None,
        metadata: _GrpcMetadata | None = None,
    ) -> None:
        self._target = target
        self._metadata = metadata
        if credentials is not None:
            self._channel = grpc.secure_channel(target, credentials)
        else:
            self._channel = grpc.insecure_channel(target)
        self._stub = pb_grpc.ArtifactServiceStub(self._channel)

    @classmethod
    def _from_channel(
        cls, channel: grpc.Channel, metadata: _GrpcMetadata | None
    ) -> ArtifactClient:
        """Internal: wrap an existing authenticated channel (no new connection)."""
        client = cls.__new__(cls)
        client._target = ""
        client._metadata = metadata
        client._channel = channel
        client._stub = pb_grpc.ArtifactServiceStub(channel)
        return client

    @classmethod
    def from_env(cls, *, chalk_client: Any | None = None) -> ArtifactClient:
        """Create an ``ArtifactClient`` from ambient Chalk credentials."""
        connect = ConnectClient(chalk_client=chalk_client)
        connect._ensure_auth()
        host, use_tls = grpc_target_from_url(connect._api_server or "")
        return cls(
            host,
            credentials=grpc.ssl_channel_credentials() if use_tls else None,
            metadata=[
                ("authorization", f"Bearer {connect._access_token}"),
                ("x-chalk-env-id", connect._env_id or ""),
                ("x-chalk-server", "go-api"),
            ],
        )

    def __enter__(self) -> ArtifactClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the gRPC channel."""
        self._channel.close()

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
            producer=pb.ARTIFACT_PRODUCER_USER,
            content_type=content_type,
            inline=pb.ArtifactInlineContent(data=data),
        )
        try:
            resp = self._stub.CreateArtifact(
                pb.CreateArtifactRequest(artifact=spec), metadata=self._metadata
            )
        except grpc.RpcError as e:
            _raise_artifact_error(e, name)
            raise
        return _artifact_from_proto(resp.artifact, self)

    @_instrumented("chalkcompute.artifact.get")
    def get(self, artifact_id: str) -> Artifact:
        """Fetch a single artifact by id."""
        try:
            resp = self._stub.GetArtifact(
                pb.GetArtifactRequest(artifact_id=artifact_id), metadata=self._metadata
            )
        except grpc.RpcError as e:
            _raise_artifact_error(e, artifact_id)
            raise
        return _artifact_from_proto(resp.artifact, self)

    @_instrumented("chalkcompute.artifact.list")
    def list(
        self,
        *,
        kinds: list[str] | None = None,
        producers: list[str] | None = None,
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
                resp = self._stub.ListArtifacts(req, metadata=self._metadata)
            except grpc.RpcError as e:
                _raise_artifact_error(e, "list")
                raise
            for msg in resp.artifacts:
                yield _artifact_from_proto(msg, self)
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return
            if not resp.HasField("next_cursor") or not resp.next_cursor:
                return
            cursor = resp.next_cursor

    @_instrumented("chalkcompute.artifact.download")
    def download(self, artifact_id: str) -> bytes:
        """Download an artifact's content bytes."""
        chunks: list[bytes] = []
        try:
            for frame in self._stub.DownloadArtifact(
                pb.DownloadArtifactRequest(artifact_id=artifact_id),
                metadata=self._metadata,
            ):
                chunks.append(frame.data)
        except grpc.RpcError as e:
            _raise_artifact_error(e, artifact_id)
            raise
        return b"".join(chunks)

    @_instrumented("chalkcompute.artifact.delete")
    def delete(self, artifact_id: str) -> None:
        """Delete an artifact and its stored content."""
        try:
            self._stub.DeleteArtifact(
                pb.DeleteArtifactRequest(artifact_id=artifact_id),
                metadata=self._metadata,
            )
        except grpc.RpcError as e:
            _raise_artifact_error(e, artifact_id)
            raise


def _raise_artifact_error(e: grpc.RpcError, subject: str) -> None:
    code = e.code() if callable(getattr(e, "code", None)) else None
    if code == grpc.StatusCode.NOT_FOUND:
        raise ArtifactNotFoundError(f"artifact not found: {subject}") from e
    raise ArtifactError(f"artifact request failed for {subject}: {e}") from e
