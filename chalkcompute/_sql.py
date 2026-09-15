"""Run Chalk SQL without importing ChalkPy or local feature definitions."""

from __future__ import annotations

import importlib
import math
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from connectrpc.errors import ConnectError
from connectrpc.code import Code
from protobuf import Oneof
from pyqwest import SyncClient

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.common.v1 import chalk_error_pb, resources_pb
from chalkcompute._gen.chalk.protosql.v1 import (
    sql_service_connect,
    sql_service_pb as pb,
)
from chalkcompute._gen.chalk.server.v1 import sql_queries_connect, sql_queries_pb
from chalkcompute._http import HTTP_TRANSPORT_ERRORS
from chalkcompute._tracing import _instrumented


class SQLError(Exception):
    """A transport, execution, or result error, with the query ID when known."""

    def __init__(
        self,
        message: str,
        *,
        query_id: str | None = None,
        code: Code | None = None,
    ) -> None:
        super().__init__(message)
        self.query_id = query_id
        self.code = code


class SQLJobStatus(str, Enum):
    """Last observed job state. The polling API combines queued/running jobs."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _positive(value: float, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a positive finite number")


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _check_errors(
    errors: Sequence[chalk_error_pb.ChalkError], query_id: str | None
) -> None:
    if errors:
        raise SQLError(
            "; ".join(error.message or str(error.code) for error in errors),
            query_id=query_id,
        )


@dataclass
class SQLResult:
    """Completed query output; conversion loads all rows into local memory.

    Use :meth:`download` to stream persisted Parquet files to disk without
    installing PyArrow. Signed URLs are refreshed before downloading older
    results. Keep the originating client open while using this handle.
    """

    query_id: str
    _client: SQLClient = field(repr=False)
    _parquet: bytes | None = field(default=None, repr=False)
    _urls: tuple[str, ...] = field(default=(), repr=False)
    _urls_at: float = field(default_factory=time.monotonic, repr=False)

    def download(self, directory: str | Path, *, timeout: float = 300.0) -> list[Path]:
        """Write every result shard as ``part-NNNNN.parquet`` without overwriting.

        ``timeout`` is the per-file HTTP timeout. Completed files remain if a
        later shard fails; a partially downloaded file is removed.
        """
        _positive(timeout, "timeout")
        destination = Path(directory)
        destination.mkdir(parents=True, exist_ok=True)
        if self._parquet is not None:
            path = destination / "part-00000.parquet"
            with path.open("xb") as output:
                output.write(self._parquet)
            return [path]

        if time.monotonic() - self._urls_at >= 300:
            self._refresh_urls(timeout=min(timeout, 30.0))
        if not self._urls:
            raise SQLError("SQL result has no Parquet files", query_id=self.query_id)
        paths: list[Path] = []
        for index, url in enumerate(self._urls):
            path = destination / f"part-{index:05d}.parquet"
            # Exclusive creation also ensures cleanup cannot remove a preexisting file.
            output = path.open("xb")
            try:
                with (
                    output,
                    self._client._http.stream("GET", url, timeout=timeout) as response,
                ):
                    if response.status >= 300:
                        raise SQLError(
                            f"SQL result download failed (HTTP {response.status})",
                            query_id=self.query_id,
                        )
                    for chunk in response.content:
                        output.write(chunk)
            except (TimeoutError, *HTTP_TRANSPORT_ERRORS):
                path.unlink(missing_ok=True)
                # Transport exceptions may contain the signed URL; do not expose it.
                raise SQLError(
                    "SQL result download failed", query_id=self.query_id
                ) from None
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            paths.append(path)
        return paths

    def _refresh_urls(self, *, timeout: float) -> None:
        stub = self._client._connect.stub(
            sql_queries_connect.SqlQueriesServiceClientSync
        )
        response = self._client._call(
            stub.get_sql_query_signed_urls,
            sql_queries_pb.GetSqlQuerySignedUrlsRequest(query_id=self.query_id),
            timeout=timeout,
            query_id=self.query_id,
        )
        urls = tuple(response.signed_result_urls)
        if not urls or any(not url for url in urls):
            raise SQLError("SQL result has no Parquet files", query_id=self.query_id)
        self._urls = urls
        self._urls_at = time.monotonic()

    def to_arrow(self) -> Any:
        """Load all result shards as a PyArrow table (requires ``pyarrow``)."""
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise ImportError("Install pyarrow to use SQLResult.to_arrow()") from exc
        if self._parquet is not None:
            return pq.read_table(pa.BufferReader(self._parquet))
        with tempfile.TemporaryDirectory(prefix="chalk-sql-") as directory:
            paths = self.download(directory)
            return pa.concat_tables([pq.read_table(path) for path in paths])

    def to_pandas(self) -> Any:
        """Load all rows as a Pandas dataframe (requires PyArrow and Pandas)."""
        try:
            importlib.import_module("pandas")
        except ImportError as exc:
            raise ImportError("Install pandas to use SQLResult.to_pandas()") from exc
        return self.to_arrow().to_pandas()


@dataclass
class SQLJob:
    """A background SQL job. ``wait`` timing out does not cancel execution."""

    query_id: str
    _client: SQLClient = field(repr=False)
    status: SQLJobStatus = field(default=SQLJobStatus.PENDING, init=False)
    _submitted_here: bool = field(default=False, repr=False)
    _observed: bool = field(default=False, init=False, repr=False)
    _result: SQLResult | None = field(default=None, init=False, repr=False)
    _error: SQLError | None = field(default=None, init=False, repr=False)

    def refresh(self, *, timeout: float = 30.0) -> SQLJob:
        """Poll once and update ``status``; failed jobs raise from ``wait``."""
        _positive(timeout, "timeout")
        if self.status in (SQLJobStatus.SUCCEEDED, SQLJobStatus.FAILED):
            return self
        stub = self._client._connect.stub(sql_service_connect.SqlServiceClientSync)
        try:
            response = self._client._call(
                stub.poll_sql_query,
                pb.PollSqlQueryRequest(operation_id=self.query_id),
                timeout=timeout,
                query_id=self.query_id,
            )
        except SQLError as exc:
            # Submission returns before the worker creates its sql_queries row.
            # Only a job this client actually submitted may treat NOT_FOUND as pending.
            if (
                exc.code == Code.NOT_FOUND
                and self._submitted_here
                and not self._observed
            ):
                return self
            raise
        if response.info is not None and response.info.operation_id != self.query_id:
            raise SQLError(
                "SQL poll returned a different query ID", query_id=self.query_id
            )
        self._observed = True
        match response.result:
            case Oneof(field="progress"):
                self.status = SQLJobStatus.RUNNING
            case Oneof(field="response", value=payload):
                self._result = self._client._result(payload, self.query_id)
                self.status = SQLJobStatus.SUCCEEDED
            case Oneof(field="failed", value=failed):
                self.status = SQLJobStatus.FAILED
                self._error = SQLError(
                    "; ".join(
                        error.message or str(error.code) for error in failed.errors
                    )
                    or "SQL query failed",
                    query_id=self.query_id,
                )
            case _:
                raise SQLError("SQL poll returned no job state", query_id=self.query_id)
        return self

    def wait(self, *, timeout: float = 600.0, poll_interval: float = 2.0) -> SQLResult:
        """Wait for completion, returning the result or raising ``SQLError``.

        Raises ``TimeoutError`` when the waiting deadline expires. The remote
        job keeps running and this handle can be waited on again.
        """
        _positive(timeout, "timeout")
        _positive(poll_interval, "poll_interval")
        deadline = time.monotonic() + timeout
        while True:
            if self._error is not None:
                raise self._error
            if self._result is not None:
                return self._result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for SQL query {self.query_id}")
            try:
                self.refresh(timeout=min(30.0, remaining))
            except SQLError as exc:
                if exc.code not in (Code.UNAVAILABLE, Code.DEADLINE_EXCEEDED):
                    raise
            if self.status not in (SQLJobStatus.SUCCEEDED, SQLJobStatus.FAILED):
                time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))


class SQLClient:
    """Chalk SQL client using ambient Chalk credentials, without ChalkPy.

    Reads client credentials from environment variables or ``~/.chalk.yml``;
    also supports Compute service identity and rotating web identity tokens.
    """

    def __init__(self, *, connect_client: ConnectClient | None = None) -> None:
        self._connect = (
            connect_client if connect_client is not None else ConnectClient()
        )
        self._owns_connect = connect_client is None
        self._http = SyncClient()

    def __enter__(self) -> SQLClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close owned RPC clients. Injected ConnectClients remain caller-owned."""
        if self._owns_connect:
            self._connect.close()

    def _call(
        self,
        method: Callable[..., Any],
        request: Any,
        *,
        timeout: float,
        query_id: str | None = None,
    ) -> Any:
        try:
            return self._connect.call(method, request, timeout=timeout)
        except ConnectError as exc:
            raise SQLError(str(exc), query_id=query_id, code=exc.code) from exc

    @_instrumented("chalkcompute.sql.query")
    def query(
        self,
        sql: str,
        *,
        resource_group: str | None = None,
        persist: bool = False,
        timeout: float = 300.0,
    ) -> SQLResult:
        """Execute synchronously. ``persist=True`` writes output to object storage.

        Persistence avoids returning the entire Parquet result in one RPC;
        it does not create a named dataset. Use ``submit`` for long-running SQL.
        Execution is never automatically retried after a transport failure.
        """
        _positive(timeout, "timeout")
        _text(sql, "sql")
        if resource_group is not None:
            _text(resource_group, "resource_group")
        if not isinstance(persist, bool):
            raise ValueError("persist must be a bool")
        options = pb.ExecuteSqlSyncQueryRequestOptions()
        if resource_group is not None:
            options.resource_group = resource_group
        stub = self._connect.stub(sql_service_connect.SqlServiceClientSync)
        response = self._call(
            stub.execute_sql_query,
            pb.ExecuteSqlQueryRequest(
                query=sql,
                persistence_settings=pb.ExecuteSqlResultPersistenceSettings(
                    enabled=persist
                ),
                options=Oneof("sync_options", options),
            ),
            timeout=timeout,
        )
        _check_errors(response.errors, response.query_id or None)
        query_id = response.query_id
        if not query_id:
            raise SQLError("SQL query returned no query ID")
        match response.payload:
            case Oneof(field="parquet", value=parquet):
                return self._result(
                    pb.ExecuteSqlSyncQueryResponsePayload(
                        payload=Oneof("parquet_response", parquet)
                    ),
                    query_id,
                )
            case Oneof(field="sync_payload", value=payload):
                return self._result(payload, query_id)
            case _:
                raise SQLError(
                    "SQL query returned no synchronous result", query_id=query_id
                )

    @_instrumented("chalkcompute.sql.submit")
    def submit(
        self,
        sql: str,
        *,
        resource_group: str | None = None,
        timeout: float = 30.0,
    ) -> SQLJob:
        """Submit a background job; ``timeout`` limits submission, not execution."""
        _positive(timeout, "timeout")
        _text(sql, "sql")
        options = pb.ExecuteSqlAsyncQueryRequestOptions(
            execution_mode=pb.ExecuteSqlAsyncExecutionMode.ASYNC,
        )
        if resource_group is not None:
            options.resources = resources_pb.ResourceRequests(
                resource_group=_text(resource_group, "resource_group"),
            )
        stub = self._connect.stub(sql_service_connect.SqlServiceClientSync)
        response = self._call(
            stub.execute_sql_query,
            pb.ExecuteSqlQueryRequest(
                query=sql,
                persistence_settings=pb.ExecuteSqlResultPersistenceSettings(
                    enabled=True
                ),
                options=Oneof("async_options", options),
            ),
            timeout=timeout,
        )
        _check_errors(response.errors, response.query_id or None)
        match response.payload:
            case Oneof(field="async_payload", value=payload) if payload.operation_id:
                if response.query_id and response.query_id != payload.operation_id:
                    raise SQLError(
                        "SQL submission returned inconsistent query IDs",
                        query_id=response.query_id,
                    )
                return SQLJob(payload.operation_id, self, _submitted_here=True)
            case _:
                raise SQLError(
                    "SQL submission returned no job ID",
                    query_id=response.query_id or None,
                )

    def get_job(self, query_id: str) -> SQLJob:
        """Attach to an existing persisted SQL job by ID; poll with ``refresh``."""
        return SQLJob(_text(query_id, "query_id"), self)

    @_instrumented("chalkcompute.sql.explain")
    def explain(self, sql: str, *, timeout: float = 30.0) -> str:
        """Return the logical query plan without executing the query."""
        _positive(timeout, "timeout")
        stub = self._connect.stub(sql_service_connect.SqlServiceClientSync)
        response = self._call(
            stub.plan_sql_query,
            pb.PlanSqlQueryRequest(query=_text(sql, "sql")),
            timeout=timeout,
        )
        _check_errors(response.errors, None)
        return response.logical_plan

    def _result(
        self, payload: pb.ExecuteSqlSyncQueryResponsePayload, query_id: str
    ) -> SQLResult:
        match payload.payload:
            case Oneof(field="parquet_response", value=parquet) if parquet:
                return SQLResult(query_id, self, _parquet=parquet)
            case Oneof(field="signed_output_uris", value=signed) if signed.uris and all(
                signed.uris
            ):
                return SQLResult(query_id, self, _urls=tuple(signed.uris))
            case _:
                raise SQLError(
                    "SQL result has no Parquet data or download URLs", query_id=query_id
                )
