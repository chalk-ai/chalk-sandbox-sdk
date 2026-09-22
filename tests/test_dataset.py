from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from protobuf import Oneof

from chalkcompute._dataset import (
    DatasetClient,
    DatasetError,
    _PreparedDatasetFile,
)
from chalkcompute._evaluation import DatasetRevisionRef
from chalkcompute._gen.chalk.server.v1 import datasets_pb
from chalkcompute._gen.chalk.volume.v1 import volume_pb
from tests._fakes import FakeConnectClient


class _FakeHTTPClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str], bytes, float | None]] = []
        self.responses: list[Any] = []

    def put(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        content: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        return self._request("PUT", url, headers, content, timeout)

    def post(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        content: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        return self._request("POST", url, headers, content, timeout)

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        return self._request("GET", url, headers, None, timeout)

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None,
        content: Any,
        timeout: float | None,
    ) -> Any:
        body = content if isinstance(content, bytes) else b"".join(content or ())
        self.requests.append((method, url, dict(headers or {}), body, timeout))
        if self.responses:
            return self.responses.pop(0)
        return _response()


def _response(
    status: int = 200,
    *,
    headers: dict[str, str] | None = None,
    content: bytes = b"",
) -> Any:
    return SimpleNamespace(
        status=status,
        headers=headers or {},
        content=content,
        trailers={},
    )


def _finalize_response(
    *, dataset_id: str = "dataset-1", revision_id: str = "revision-1"
) -> datasets_pb.FinalizeDatasetUploadResponse:
    return datasets_pb.FinalizeDatasetUploadResponse(
        dataset=datasets_pb.DatasetMeta(
            id=dataset_id,
            dataset_name="support_goldens",
            most_recent_revision=datasets_pb.DatasetRevisionMeta(
                dataset_id=dataset_id,
                offline_query_id=revision_id,
            ),
        )
    )


@pytest.mark.parametrize(
    "make_data",
    [list, tuple, set, frozenset, iter, lambda paths: paths[0].parent.glob("*.csv")],
    ids=["list", "tuple", "set", "frozenset", "iterator", "glob"],
)
def test_upload_csv_files_registers_schema_and_threads_session(
    tmp_path: Path,
    make_data: Callable[[list[Path]], object],
) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    first.write_text("id,score,active,name\n,1,true,alice\n2,2.5,false,bob\n")
    second.write_text("id,score,active,name\n3,4.5,true,charlie\n")
    get_requests: list[datasets_pb.GetDatasetUploadUrisRequest] = []
    finalize_requests: list[datasets_pb.FinalizeDatasetUploadRequest] = []

    def handler(method: str, request: Any) -> Any:
        if method == "get_dataset_upload_uris":
            get_requests.append(request)
            return datasets_pb.GetDatasetUploadUrisResponse(
                upload_session_id="session-1",
                upload=Oneof(
                    "direct",
                    volume_pb.DirectUpload(
                        signed_upload_uri=f"https://upload/{len(get_requests)}"
                    ),
                ),
            )
        if method == "finalize_dataset_upload":
            finalize_requests.append(request)
            return _finalize_response()
        raise AssertionError(method)

    connect = FakeConnectClient(handler=handler)
    http = _FakeHTTPClient()
    progress: list[tuple[int, int]] = []
    result = DatasetClient(
        connect_client=connect,  # type: ignore[arg-type]
        http_client=http,
    ).upload(
        "  support_goldens  ",
        make_data([first, second]),
        part_size_bytes=1024,
        timeout=12,
        on_progress=lambda uploaded, total: progress.append((uploaded, total)),
    )

    assert result == DatasetRevisionRef("dataset-1", "revision-1")
    assert len(get_requests) == 2
    assert get_requests[0].content_size > 0
    assert get_requests[0].part_size == 1024
    assert not get_requests[0].has_field("upload_session_id")
    assert get_requests[1].upload_session_id == "session-1"
    assert [request[1] for request in http.requests] == [
        "https://upload/1",
        "https://upload/2",
    ]
    assert all(body.startswith(b"PAR1") for _, _, _, body, _ in http.requests)
    assert all(body.endswith(b"PAR1") for _, _, _, body, _ in http.requests)
    tables = [pq.read_table(pa.BufferReader(request[3])) for request in http.requests]
    assert sorted(pa.concat_tables(tables).column("name").to_pylist()) == [
        "alice",
        "bob",
        "charlie",
    ]
    assert first.exists() and second.exists()
    assert progress == [(1, 2), (2, 2)]

    finalize = finalize_requests[0]
    assert finalize.upload_session_id == "session-1"
    assert finalize.dataset_name == "support_goldens"
    schema = pa.ipc.read_schema(pa.BufferReader(finalize.arrow_schema_serialized))
    assert schema.names == ["id", "score", "active", "name"]
    assert schema.types == [pa.int64(), pa.float64(), pa.bool_(), pa.string()]


@pytest.mark.parametrize("path_type", [str, Path])
@pytest.mark.parametrize("upload_variant", ["direct", "resumable"])
def test_upload_single_parquet_preserves_file(
    tmp_path: Path, path_type: Callable[[Path], object], upload_variant: str
) -> None:
    path = tmp_path / "source.parquet"
    table = pa.table({"id": [1, 2], "value": ["a", "b"]})
    pq.write_table(table, path)
    source_bytes = path.read_bytes()
    http = _FakeHTTPClient()

    def handler(method: str, request: Any) -> Any:
        if method == "get_dataset_upload_uris":
            target = (
                volume_pb.DirectUpload(signed_upload_uri="https://upload/file")
                if upload_variant == "direct"
                else volume_pb.ResumableUpload(signed_upload_uri="https://upload/file")
            )
            return datasets_pb.GetDatasetUploadUrisResponse(
                upload_session_id="session-1",
                upload=Oneof(upload_variant, target),
            )
        if method == "finalize_dataset_upload":
            return _finalize_response()
        raise AssertionError(method)

    connect = FakeConnectClient(handler=handler)
    result = DatasetClient(connect_client=connect, http_client=http).upload(  # type: ignore[arg-type]
        "single", path_type(path)
    )

    assert result == DatasetRevisionRef("dataset-1", "revision-1")
    assert [call.method for call in connect.calls] == [
        "get_dataset_upload_uris",
        "finalize_dataset_upload",
    ]
    assert connect.calls[0].request.content_size == len(source_bytes)
    assert len(http.requests) == 1
    assert http.requests[0][3] == source_bytes
    assert http.requests[0][2]["Content-Length"] == str(len(source_bytes))
    schema = pa.ipc.read_schema(
        pa.BufferReader(connect.calls[-1].request.arrow_schema_serialized)
    )
    assert schema.equals(table.schema)
    assert path.read_bytes() == source_bytes


@pytest.mark.parametrize(
    "input_kind", ["columns", "rows", "table", "batch", "reader", "dataframe"]
)
def test_upload_in_memory_data_as_parquet(input_kind: str) -> None:
    expected = {"id": [1, 2], "value": ["a", "b"]}
    table = pa.table(expected)

    class IterableDataFrame:
        def __iter__(self):
            raise AssertionError("dataframes must not be consumed as paths")

        def to_arrow(self):
            return table

    data = {
        "columns": expected,
        "rows": table.to_pylist(),
        "table": table,
        "batch": table.to_batches()[0],
        "reader": table.to_reader(),
        "dataframe": IterableDataFrame(),
    }[input_kind]
    uploaded: list[bytes] = []

    def handler(method: str, request: Any) -> Any:
        if method == "get_dataset_upload_uris":
            return datasets_pb.GetDatasetUploadUrisResponse(
                upload_session_id="session-1",
                upload=Oneof(
                    "direct",
                    volume_pb.DirectUpload(signed_upload_uri="https://upload/table"),
                ),
            )
        if method == "finalize_dataset_upload":
            return _finalize_response()
        raise AssertionError(method)

    class CapturingHTTPClient(_FakeHTTPClient):
        def put(self, *args: Any, **kwargs: Any) -> Any:
            response = super().put(*args, **kwargs)
            uploaded.append(self.requests[-1][3])
            return response

    DatasetClient(
        connect_client=FakeConnectClient(handler=handler),  # type: ignore[arg-type]
        http_client=CapturingHTTPClient(),
    ).upload("table", data)

    table = pq.read_table(pa.BufferReader(uploaded[0]))
    assert table.to_pydict() == expected


def test_uploader_has_no_chalk_feature_dependency() -> None:
    from chalkcompute import _dataset

    source = inspect.getsource(_dataset)
    assert "chalk.features" not in source
    assert "Feature" not in source


def test_upload_rejects_mismatched_schemas_before_requesting_urls(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    pq.write_table(pa.table({"id": [1]}), first)
    pq.write_table(pa.table({"name": ["alice"]}), second)
    connect = FakeConnectClient(
        handler=lambda method, request: pytest.fail(f"unexpected RPC: {method}")
    )

    with pytest.raises(DatasetError, match="does not match the first input"):
        DatasetClient(connect_client=connect).upload(  # type: ignore[arg-type]
            "mismatch", [first, second]
        )

    assert connect.calls == []


@pytest.mark.parametrize("data", [[], set(), iter(()), b"parquet", object()])
def test_upload_rejects_unsupported_or_empty_data(data: object) -> None:
    with pytest.raises((DatasetError, TypeError)):
        DatasetClient(connect_client=FakeConnectClient()).upload(  # type: ignore[arg-type]
            "invalid", data
        )


@pytest.mark.parametrize(
    "invalid_kind", ["missing", "directory", "unsupported", "schema"]
)
def test_upload_validates_all_files_and_cleans_up_before_requesting_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_kind: str
) -> None:
    first = tmp_path / "first.csv"
    first.write_text("id\n1\n")
    temporary = tmp_path / "converted.parquet"
    monkeypatch.setattr(
        "chalkcompute._dataset._temporary_parquet_path", lambda: temporary
    )
    invalid = tmp_path / "invalid.parquet"
    if invalid_kind == "directory":
        invalid.mkdir()
    elif invalid_kind == "unsupported":
        invalid = tmp_path / "invalid.txt"
        invalid.write_text("id\n2\n")
    elif invalid_kind == "schema":
        pq.write_table(pa.table({"other": [2]}), invalid)
    connect = FakeConnectClient(
        handler=lambda method, request: pytest.fail(f"unexpected RPC: {method}")
    )
    http = _FakeHTTPClient()

    with pytest.raises(DatasetError):
        DatasetClient(connect_client=connect, http_client=http).upload(  # type: ignore[arg-type]
            "invalid", iter([first, invalid])
        )

    assert connect.calls == []
    assert http.requests == []
    assert not temporary.exists()
    assert first.exists()


def test_multipart_upload_sends_parts_and_completion_xml(tmp_path: Path) -> None:
    path = tmp_path / "data.parquet"
    path.write_bytes(b"abcdefghij")
    http = _FakeHTTPClient()
    http.responses = [
        _response(headers={"ETag": '"etag-1"'}),
        _response(headers={"ETag": '"etag-2"'}),
        _response(),
    ]
    client = DatasetClient(connect_client=FakeConnectClient(), http_client=http)  # type: ignore[arg-type]
    client._upload_file(
        _prepared(path),
        datasets_pb.GetDatasetUploadUrisResponse(
            upload_session_id="session-1",
            upload=Oneof(
                "multipart",
                volume_pb.MultipartUpload(
                    signed_upload_uris=[
                        "https://upload/part-1",
                        "https://upload/part-2",
                        "https://upload/complete",
                    ]
                ),
            ),
        ),
        timeout=30,
    )

    assert [request[3] for request in http.requests[:2]] == [b"abcde", b"fghij"]
    assert http.requests[2][0:2] == ("POST", "https://upload/complete")
    root = ElementTree.fromstring(http.requests[2][3])
    assert [node.text for node in root.findall("./Part/PartNumber")] == ["1", "2"]
    assert [node.text for node in root.findall("./Part/ETag")] == [
        '"etag-1"',
        '"etag-2"',
    ]


def test_azure_block_upload_stages_and_commits_blocks(tmp_path: Path) -> None:
    path = tmp_path / "data.parquet"
    path.write_bytes(b"abcdefghij")
    http = _FakeHTTPClient()
    client = DatasetClient(connect_client=FakeConnectClient(), http_client=http)  # type: ignore[arg-type]
    client._upload_file(
        _prepared(path),
        datasets_pb.GetDatasetUploadUrisResponse(
            upload_session_id="session-1",
            upload=Oneof(
                "azure_block",
                volume_pb.AzureBlockUpload(
                    signed_blob_uri="https://upload/blob?sig=secret",
                    block_id_prefix="block prefix",
                    part_size=4,
                ),
            ),
        ),
        timeout=30,
    )

    assert [request[3] for request in http.requests[:3]] == [
        b"abcd",
        b"efgh",
        b"ij",
    ]
    assert "blockid=block%20prefix-0000000000" in http.requests[0][1]
    assert http.requests[-1][1].endswith("&comp=blocklist")
    root = ElementTree.fromstring(http.requests[-1][3])
    assert [node.text for node in root.findall("./Latest")] == [
        "block prefix-0000000000",
        "block prefix-0000000001",
        "block prefix-0000000002",
    ]


def test_signed_upload_http_error_is_wrapped_without_exposing_url(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data.parquet"
    path.write_bytes(b"payload")
    http = _FakeHTTPClient()
    http.responses = [_response(403, content=b"signature expired")]
    client = DatasetClient(connect_client=FakeConnectClient(), http_client=http)  # type: ignore[arg-type]

    with pytest.raises(DatasetError, match="HTTP 403: signature expired") as raised:
        client._upload_file(
            _prepared(path),
            datasets_pb.GetDatasetUploadUrisResponse(
                upload_session_id="session-1",
                upload=Oneof(
                    "direct",
                    volume_pb.DirectUpload(
                        signed_upload_uri="https://upload/file?secret=yes"
                    ),
                ),
            ),
            timeout=30,
        )

    assert "secret=yes" not in str(raised.value)


def _prepared(path: Path) -> _PreparedDatasetFile:
    return _PreparedDatasetFile(
        source_path=path,
        upload_path=path,
        size=path.stat().st_size,
        schema=pa.schema([pa.field("value", pa.string())]),
    )


def _download_links(*urls: str) -> Any:
    return datasets_pb.GetDatasetRevisionDownloadLinksResponse(output_urls=list(urls))


def test_read_returns_the_revision_as_one_arrow_table() -> None:
    table = pa.table({"a": [1, 2], "output": ["x", "y"]})
    buffer = pa.BufferOutputStream()
    pq.write_table(table, buffer)
    parquet = buffer.getvalue().to_pybytes()

    http = _FakeHTTPClient()
    http.responses = [_response(content=parquet)]
    connect = FakeConnectClient(
        handler=lambda name, request: _download_links("https://signed/part-0")
    )

    read = DatasetClient(connect_client=connect, http_client=http).read("rev-1")  # type: ignore[arg-type]

    assert read.equals(table)
    assert [request[0] for request in http.requests] == ["GET"]


def test_read_concatenates_a_multi_file_revision_and_takes_a_ref() -> None:
    first = pa.table({"a": [1]})
    second = pa.table({"a": [2]})
    parts = []
    for part in (first, second):
        buffer = pa.BufferOutputStream()
        pq.write_table(part, buffer)
        parts.append(_response(content=buffer.getvalue().to_pybytes()))

    http = _FakeHTTPClient()
    http.responses = parts
    connect = FakeConnectClient(
        handler=lambda name, request: _download_links("https://a", "https://b")
    )

    read = DatasetClient(connect_client=connect, http_client=http).read(  # type: ignore[arg-type]
        DatasetRevisionRef(dataset_id="d", revision_id="rev-1")
    )

    assert read.equals(pa.concat_tables([first, second]))


def test_read_rejects_an_empty_revision_and_a_revision_with_no_output() -> None:
    with pytest.raises(ValueError, match="revision_id is required"):
        DatasetClient(connect_client=FakeConnectClient()).read("  ")  # type: ignore[arg-type]

    connect = FakeConnectClient(handler=lambda name, request: _download_links())
    with pytest.raises(DatasetError, match="no output to read"):
        DatasetClient(connect_client=connect).read("rev-1")  # type: ignore[arg-type]
