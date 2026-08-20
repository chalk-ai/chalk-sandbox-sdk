from __future__ import annotations

import inspect
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


def test_upload_csv_files_registers_schema_and_threads_session(
    tmp_path: Path,
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
        [first, second],
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
    assert progress == [(1, 2), (2, 2)]

    finalize = finalize_requests[0]
    assert finalize.upload_session_id == "session-1"
    assert finalize.dataset_name == "support_goldens"
    schema = pa.ipc.read_schema(pa.BufferReader(finalize.arrow_schema_serialized))
    assert schema.names == ["id", "score", "active", "name"]
    assert schema.types == [pa.int64(), pa.float64(), pa.bool_(), pa.string()]


def test_upload_in_memory_column_data_as_parquet(tmp_path: Path) -> None:
    del tmp_path
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
    ).upload("table", {"id": [1, 2], "value": ["a", "b"]})

    table = pq.read_table(pa.BufferReader(uploaded[0]))
    assert table.to_pydict() == {"id": [1, 2], "value": ["a", "b"]}


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


@pytest.mark.parametrize("data", [[], b"parquet", object()])
def test_upload_rejects_unsupported_or_empty_data(data: object) -> None:
    with pytest.raises((DatasetError, TypeError)):
        DatasetClient(connect_client=FakeConnectClient()).upload(  # type: ignore[arg-type]
            "invalid", data
        )


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
