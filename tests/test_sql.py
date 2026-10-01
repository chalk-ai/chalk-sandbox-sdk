from __future__ import annotations

import io
from contextlib import contextmanager
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from protobuf import Oneof

from chalkcompute import SQLClient, SQLError, SQLJobStatus
from chalkcompute._gen.chalk.common.v1 import chalk_error_pb
from chalkcompute._gen.chalk.protosql.v1 import sql_service_pb as pb
from chalkcompute._gen.chalk.server.v1 import sql_queries_pb
from tests._fakes import FakeConnectClient


def parquet(values=(1, 2)):
    buffer = io.BytesIO()
    pq.write_table(pa.table({"n": pa.array(values, type=pa.int64())}), buffer)
    return buffer.getvalue()


def inline(data=None):
    return pb.ExecuteSqlSyncQueryResponsePayload(
        payload=Oneof("parquet_response", parquet() if data is None else data),
    )


def signed(*urls):
    return pb.ExecuteSqlSyncQueryResponsePayload(
        payload=Oneof("signed_output_uris", pb.SignedOutputUris(uris=list(urls))),
    )


def response(payload=None):
    return pb.ExecuteSqlQueryResponse(
        query_id="query-1",
        payload=Oneof("sync_payload", payload or inline()),
    )


def submitted():
    return pb.ExecuteSqlQueryResponse(
        query_id="query-1",
        payload=Oneof(
            "async_payload",
            pb.ExecuteSqlAsyncQueryResponsePayload(operation_id="query-1"),
        ),
    )


def poll(payload):
    return pb.PollSqlQueryResponse(
        info=pb.SqlQueryInfo(operation_id="query-1"),
        result=payload,
    )


def client_with(*responses):
    pending = iter(responses)

    def handler(method, request):
        # Exercise generated descriptors and nested message serialization too.
        type(request).from_binary(request.to_binary())
        value = next(pending)
        if isinstance(value, Exception):
            raise value
        return value

    connect = FakeConnectClient(handler=handler)
    return SQLClient(connect_client=connect), connect


class DownloadHTTP:
    def __init__(self, files):
        self.files = files
        self.requests = []

    @contextmanager
    def stream(self, method, url, *, timeout):
        self.requests.append((method, url, timeout))
        value = self.files[url]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, int):
            yield SimpleNamespace(status=value, content=iter(()))
        else:
            yield SimpleNamespace(status=200, content=iter([value[:10], value[10:]]))


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("values", [(), (1, 2)])
def test_query_decodes_inline_variants_and_empty_schema(legacy, values):
    data = parquet(values)
    reply = response(inline(data))
    if legacy:
        reply.payload = Oneof("parquet", data)
    client, connect = client_with(reply)
    result = client.query("SELECT n FROM t", resource_group="online", timeout=12)
    assert result.query_id == "query-1"
    table = result.to_arrow()
    assert table.to_pydict() == {"n": list(values)}
    assert table.schema.field("n").type == pa.int64()
    request = connect.calls[0].request
    assert request.query == "SELECT n FROM t"
    assert request.options.value.resource_group == "online"
    assert not request.persistence_settings.enabled
    assert connect.calls[0].timeout == 12


def test_download_all_shards_and_convert(tmp_path):
    client, connect = client_with(
        response(signed("https://store/a", "https://store/b"))
    )
    http = DownloadHTTP(
        {"https://store/a": parquet([1]), "https://store/b": parquet([2])}
    )
    client._http = http
    result = client.query("SELECT n FROM t", persist=True)
    assert connect.calls[0].request.persistence_settings.enabled
    assert result.to_arrow().to_pydict() == {"n": [1, 2]}
    paths = result.download(tmp_path)
    assert len(paths) == 2
    assert all(path.read_bytes().startswith(b"PAR1") for path in paths)
    with pytest.raises(FileExistsError):
        result.download(tmp_path)
    assert paths[0].read_bytes() == parquet([1])


def test_expired_urls_are_refreshed_without_rerunning_sql(tmp_path):
    client, connect = client_with(
        response(signed("https://store/expired")),
        sql_queries_pb.GetSqlQuerySignedUrlsResponse(
            signed_result_urls=["https://store/fresh"]
        ),
    )
    client._http = DownloadHTTP({"https://store/fresh": parquet()})
    result = client.query("SELECT 1", persist=True)
    result._urls_at -= 600
    result.download(tmp_path)
    assert [call.method for call in connect.calls] == [
        "execute_sql_query",
        "get_sql_query_signed_urls",
    ]
    assert connect.calls[1].request.query_id == "query-1"


@pytest.mark.parametrize(
    "failure", [403, ConnectionError("https://store/a?signature=secret")]
)
def test_download_failure_removes_partial_file_and_redacts_url(tmp_path, failure):
    client, _ = client_with(response(signed("https://store/a?signature=secret")))
    client._http = DownloadHTTP({"https://store/a?signature=secret": failure})
    result = client.query("SELECT 1", persist=True)
    with pytest.raises(SQLError) as error:
        result.download(tmp_path)
    assert error.value.query_id == "query-1"
    assert "secret" not in str(error.value)
    assert list(tmp_path.iterdir()) == []
    assert "secret" not in repr(result)


def test_inline_download_does_not_overwrite(tmp_path):
    client, _ = client_with(response())
    result = client.query("SELECT 1")
    path = result.download(tmp_path)[0]
    with pytest.raises(FileExistsError):
        result.download(tmp_path)
    assert path.read_bytes() == parquet()


def test_submit_wait_tolerates_worker_startup_then_completes(monkeypatch):
    client, connect = client_with(
        submitted(),
        ConnectError(Code.NOT_FOUND, "worker has not started"),
        poll(Oneof("progress", pb.SqlQueryProgressInfo())),
        poll(Oneof("response", inline())),
    )
    monkeypatch.setattr("chalkcompute._sql.time.sleep", lambda _: None)
    job = client.submit("SELECT 1", resource_group="batch", timeout=10)
    request = connect.calls[0].request
    assert request.options.field == "async_options"
    assert request.options.value.resources.resource_group == "batch"
    assert request.persistence_settings.enabled
    assert job.status == SQLJobStatus.PENDING
    result = job.wait()
    assert result.query_id == job.query_id
    assert job.status == SQLJobStatus.SUCCEEDED
    assert job.wait() is result  # no extra polling after completion
    assert len(connect.calls) == 4


def test_failed_job_preserves_error_and_query_id():
    client, _ = client_with(
        poll(
            Oneof(
                "failed",
                pb.SqlQueryFailedInfo(
                    errors=[chalk_error_pb.ChalkError(message="source unavailable")],
                ),
            )
        )
    )
    job = client.get_job("query-1").refresh()
    assert job.status == SQLJobStatus.FAILED
    with pytest.raises(SQLError, match="source unavailable") as error:
        job.wait()
    assert error.value.query_id == "query-1"


def test_resumed_job_not_found_is_not_silently_pending():
    client, _ = client_with(ConnectError(Code.NOT_FOUND, "missing"))
    with pytest.raises(SQLError) as error:
        client.get_job("query-1").refresh()
    assert error.value.code == Code.NOT_FOUND


def test_wait_timeout_can_be_resumed(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("chalkcompute._sql.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "chalkcompute._sql.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    client, connect = client_with(
        poll(Oneof("progress", pb.SqlQueryProgressInfo())),
        poll(Oneof("response", inline())),
    )
    job = client.get_job("query-1")
    with pytest.raises(TimeoutError, match="query-1"):
        job.wait(timeout=0.5, poll_interval=2)
    assert connect.calls[0].timeout == 0.5
    assert job.wait().query_id == "query-1"


def test_explain_and_application_errors():
    client, _ = client_with(
        pb.PlanSqlQueryResponse(logical_plan="TableScan"),
        pb.PlanSqlQueryResponse(errors=[chalk_error_pb.ChalkError(message="bad SQL")]),
        pb.ExecuteSqlQueryResponse(
            query_id="query-1", errors=[chalk_error_pb.ChalkError(message="denied")]
        ),
    )
    assert client.explain("SELECT 1") == "TableScan"
    with pytest.raises(SQLError, match="bad SQL"):
        client.explain("SELECT nonsense")
    with pytest.raises(SQLError, match="denied") as error:
        client.query("SELECT secret")
    assert error.value.query_id == "query-1"


@pytest.mark.parametrize(
    "reply",
    [
        pb.ExecuteSqlQueryResponse(query_id="query-1"),
        response(pb.ExecuteSqlSyncQueryResponsePayload()),
        response(signed()),
        response(inline(b"")),
        pb.ExecuteSqlQueryResponse(),
        submitted(),
    ],
)
def test_query_rejects_missing_or_wrong_payload(reply):
    client, _ = client_with(reply)
    with pytest.raises(SQLError):
        client.query("SELECT 1")


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True])
def test_invalid_timeouts_fail_before_rpc(value):
    client, connect = client_with()
    for method in (client.query, client.submit, client.explain):
        with pytest.raises(ValueError):
            method("SELECT 1", timeout=value)
    assert not connect.calls


@pytest.mark.parametrize("method", ["query", "submit", "explain"])
def test_blank_sql_fails_before_rpc(method):
    client, connect = client_with()
    with pytest.raises(ValueError):
        getattr(client, method)("  ")
    assert not connect.calls


def test_pandas_conversion():
    pytest.importorskip("pandas")
    client, _ = client_with(response())
    assert client.query("SELECT 1").to_pandas()["n"].tolist() == [1, 2]


def test_wait_retries_transient_poll_errors_within_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("chalkcompute._sql.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "chalkcompute._sql.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    client, connect = client_with(
        ConnectError(Code.UNAVAILABLE, "engine restarting"),
        ConnectError(Code.DEADLINE_EXCEEDED, "timeout"),
    )
    with pytest.raises(TimeoutError, match="query-1"):
        client.get_job("query-1").wait(timeout=3, poll_interval=2)
    assert [call.timeout for call in connect.calls] == [3, 1]
