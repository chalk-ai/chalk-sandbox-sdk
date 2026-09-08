"""Upload ordinary tabular data as Chalk dataset revisions.

The upload surface is intentionally independent of Chalk feature definitions.
It accepts CSV/Parquet files or common in-memory tabular values, converts them
to Parquet when needed, and registers their Arrow schema with the dataset
service.
"""

from __future__ import annotations

import csv
import math
import os
import tempfile
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
from xml.etree import ElementTree

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf import Oneof
from pyqwest import SyncClient

from chalkcompute._connect import ConnectClient
from chalkcompute._evaluation import DatasetRevisionRef
from chalkcompute._gen.chalk.server.v1 import datasets_connect, datasets_pb
from chalkcompute._http import HTTPResponse, HTTP_TRANSPORT_ERRORS
from chalkcompute._tracing import _instrumented

_DEFAULT_PART_SIZE_BYTES = 16 * 1024 * 1024
_TRANSFER_CHUNK_SIZE_BYTES = 1024 * 1024
_CSV_BLOCK_SIZE_BYTES = 64 * 1024
_MAX_ERROR_BODY_BYTES = 4096


class DatasetError(Exception):
    """Raised when dataset preparation, upload, or registration fails."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _UploadHTTPClient(Protocol):
    def put(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        content: bytes | Iterable[bytes] | None = None,
        *,
        timeout: float | None = None,
    ) -> HTTPResponse[bytes]: ...

    def post(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        content: bytes | Iterable[bytes] | None = None,
        *,
        timeout: float | None = None,
    ) -> HTTPResponse[bytes]: ...


@dataclass(frozen=True)
class _PreparedDatasetFile:
    source_path: Path
    upload_path: Path
    size: int
    schema: Any
    temporary: bool = False


class DatasetClient:
    """Client for uploading data into named Chalk datasets."""

    def __init__(
        self,
        *,
        chalk_client: Any | None = None,
        connect_client: ConnectClient | None = None,
        http_client: _UploadHTTPClient | None = None,
    ) -> None:
        if chalk_client is not None and connect_client is not None:
            raise ValueError("pass only one of chalk_client or connect_client")
        self._connect = connect_client or ConnectClient(chalk_client=chalk_client)
        # Signed object-store URLs are not ConnectRPC endpoints. In particular,
        # do not use the SDK's HTTP/2-only RPC transport for these PUTs.
        self._http = http_client or SyncClient()

    @_instrumented("chalkcompute.dataset.upload")
    def upload(
        self,
        name: str,
        data: object,
        *,
        part_size_bytes: int = _DEFAULT_PART_SIZE_BYTES,
        timeout: float = 300.0,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> DatasetRevisionRef:
        """Upload data and return the registered dataset revision.

        ``data`` may be one CSV/Parquet path, an iterable of CSV/Parquet paths
        (including lists, sets, and ``Path.glob()`` results),
        a PyArrow table or record batch, a mapping of column names to values, a
        sequence of row mappings, or a dataframe exposing ``to_arrow`` or the
        dataframe interchange protocol. CSV files and in-memory values are
        converted to temporary Parquet files locally. Multiple files must have
        identical Arrow schemas.

        If ``name`` already exists, the upload creates a new revision of that
        dataset. The returned reference can be passed directly to
        :meth:`chalkcompute.Evaluation.create`.
        """
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        if (
            not isinstance(part_size_bytes, int)
            or isinstance(part_size_bytes, bool)
            or part_size_bytes <= 0
        ):
            raise ValueError("part_size_bytes must be a positive integer")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number")

        with _prepare_dataset_files(data) as files:
            stub = self._connect.stub(datasets_connect.DatasetMetadataServiceClientSync)
            upload_session_id: str | None = None
            for index, file in enumerate(files, start=1):
                response = self._call(
                    stub.get_dataset_upload_uris,
                    datasets_pb.GetDatasetUploadUrisRequest(
                        content_size=file.size,
                        part_size=part_size_bytes,
                        upload_session_id=upload_session_id,
                    ),
                    timeout=timeout,
                )
                returned_session_id = response.upload_session_id.strip()
                if not returned_session_id:
                    raise DatasetError(
                        "GetDatasetUploadUris returned no upload_session_id"
                    )
                if (
                    upload_session_id is not None
                    and returned_session_id != upload_session_id
                ):
                    raise DatasetError(
                        "GetDatasetUploadUris changed the upload_session_id "
                        "while uploading one dataset revision"
                    )
                upload_session_id = returned_session_id
                self._upload_file(file, response, timeout=timeout)
                if on_progress is not None:
                    on_progress(index, len(files))

            if upload_session_id is None:
                raise DatasetError("at least one dataset file is required")
            final = self._call(
                stub.finalize_dataset_upload,
                datasets_pb.FinalizeDatasetUploadRequest(
                    upload_session_id=upload_session_id,
                    dataset_name=name,
                    arrow_schema_serialized=files[0].schema.serialize().to_pybytes(),
                ),
                timeout=timeout,
            )

        dataset = final.dataset
        if dataset is None:
            raise DatasetError("FinalizeDatasetUpload returned no dataset")
        revision = dataset.most_recent_revision
        if revision is None:
            raise DatasetError("FinalizeDatasetUpload returned no most_recent_revision")
        dataset_id = dataset.id or revision.dataset_id
        revision_id = revision.offline_query_id
        if not dataset_id or not revision_id:
            raise DatasetError(
                "FinalizeDatasetUpload returned an incomplete dataset revision"
            )
        return DatasetRevisionRef(dataset_id=dataset_id, revision_id=revision_id)

    def _call(self, method: Any, request: Any, *, timeout: float) -> Any:
        try:
            return self._connect.call(method, request, timeout=timeout)
        except ConnectRpcError as exc:
            raise DatasetError(
                f"{getattr(method, '__name__', 'dataset RPC')} failed: {exc}",
                status_code=_http_status_for_code(exc.code),
            ) from exc

    def _upload_file(
        self,
        file: _PreparedDatasetFile,
        response: datasets_pb.GetDatasetUploadUrisResponse,
        *,
        timeout: float,
    ) -> None:
        match response.upload:
            case Oneof(field="direct", value=direct):
                self._put_file(
                    direct.signed_upload_uri,
                    file.upload_path,
                    offset=0,
                    length=file.size,
                    timeout=timeout,
                    operation="direct upload",
                )
            case Oneof(field="resumable", value=resumable):
                self._put_file(
                    resumable.signed_upload_uri,
                    file.upload_path,
                    offset=0,
                    length=file.size,
                    timeout=timeout,
                    operation="resumable upload",
                )
            case Oneof(field="multipart", value=multipart):
                self._upload_multipart(
                    file.upload_path,
                    file.size,
                    multipart.signed_upload_uris,
                    timeout=timeout,
                )
            case Oneof(field="azure_block", value=azure):
                self._upload_azure_blocks(
                    file.upload_path,
                    file.size,
                    signed_blob_uri=azure.signed_blob_uri,
                    block_id_prefix=azure.block_id_prefix,
                    part_size=azure.part_size,
                    timeout=timeout,
                )
            case _:
                raise DatasetError("server returned no supported upload variant")

    def _put_file(
        self,
        uri: str,
        path: Path,
        *,
        offset: int,
        length: int,
        timeout: float,
        operation: str,
    ) -> HTTPResponse[bytes]:
        if not uri:
            raise DatasetError(f"{operation} returned an empty signed URL")
        try:
            response = self._http.put(
                uri,
                headers={"Content-Length": str(length)},
                content=_file_chunks(path, offset=offset, length=length),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise DatasetError(f"{operation} timed out") from exc
        except HTTP_TRANSPORT_ERRORS as exc:
            raise DatasetError(f"{operation} failed: {exc}") from exc
        _raise_for_http_status(response, operation=operation)
        return response

    def _upload_multipart(
        self,
        path: Path,
        size: int,
        signed_upload_uris: Sequence[str],
        *,
        timeout: float,
    ) -> None:
        if len(signed_upload_uris) < 2:
            raise DatasetError(
                "multipart upload requires at least one part URL and one completion URL"
            )
        part_uris = signed_upload_uris[:-1]
        completion_uri = signed_upload_uris[-1]
        part_size = (size + len(part_uris) - 1) // len(part_uris)
        completed: list[tuple[int, str]] = []
        for index, uri in enumerate(part_uris):
            offset = index * part_size
            if offset >= size:
                raise DatasetError(
                    "multipart upload returned more part URLs than the file requires"
                )
            length = min(size - offset, part_size)
            response = self._put_file(
                uri,
                path,
                offset=offset,
                length=length,
                timeout=timeout,
                operation=f"multipart part {index + 1}",
            )
            etag = response.headers.get("ETag") or response.headers.get("etag")
            if not etag:
                raise DatasetError(
                    f"multipart part {index + 1} response did not include an ETag"
                )
            completed.append((index + 1, etag))

        root = ElementTree.Element("CompleteMultipartUpload")
        for part_number, etag in completed:
            part = ElementTree.SubElement(root, "Part")
            ElementTree.SubElement(part, "PartNumber").text = str(part_number)
            ElementTree.SubElement(part, "ETag").text = etag
        payload = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        try:
            response = self._http.post(
                completion_uri,
                headers={
                    "Content-Length": str(len(payload)),
                    "Content-Type": "application/xml",
                },
                content=payload,
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise DatasetError("multipart completion timed out") from exc
        except HTTP_TRANSPORT_ERRORS as exc:
            raise DatasetError(f"multipart completion failed: {exc}") from exc
        _raise_for_http_status(response, operation="multipart completion")

    def _upload_azure_blocks(
        self,
        path: Path,
        size: int,
        *,
        signed_blob_uri: str,
        block_id_prefix: str,
        part_size: int,
        timeout: float,
    ) -> None:
        if not signed_blob_uri:
            raise DatasetError("Azure block upload returned an empty signed URL")
        block_size = part_size if part_size > 0 else _DEFAULT_PART_SIZE_BYTES
        block_ids: list[str] = []
        for index, offset in enumerate(range(0, size, block_size)):
            length = min(size - offset, block_size)
            block_id = f"{block_id_prefix}-{index:010d}"
            block_uri = _append_query(
                signed_blob_uri,
                f"comp=block&blockid={quote(block_id, safe='')}",
            )
            self._put_file(
                block_uri,
                path,
                offset=offset,
                length=length,
                timeout=timeout,
                operation=f"Azure block {index + 1}",
            )
            block_ids.append(block_id)

        root = ElementTree.Element("BlockList")
        for block_id in block_ids:
            ElementTree.SubElement(root, "Latest").text = block_id
        payload = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        commit_uri = _append_query(signed_blob_uri, "comp=blocklist")
        try:
            response = self._http.put(
                commit_uri,
                headers={
                    "Content-Length": str(len(payload)),
                    "Content-Type": "application/xml",
                },
                content=payload,
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise DatasetError("Azure block-list commit timed out") from exc
        except HTTP_TRANSPORT_ERRORS as exc:
            raise DatasetError(f"Azure block-list commit failed: {exc}") from exc
        _raise_for_http_status(response, operation="Azure block-list commit")


@contextmanager
def _prepare_dataset_files(
    data: object,
) -> Generator[list[_PreparedDatasetFile], None, None]:
    temporary_paths: list[Path] = []
    try:
        paths = _dataset_paths(data)
        if paths is None:
            path, schema = _write_tabular_data_to_parquet(data)
            temporary_paths.append(path)
            prepared = [
                _PreparedDatasetFile(
                    source_path=path,
                    upload_path=path,
                    size=path.stat().st_size,
                    schema=schema,
                    temporary=True,
                )
            ]
        else:
            if not paths:
                raise DatasetError("at least one CSV or Parquet file is required")
            prepared = []
            for path in paths:
                item = _prepare_dataset_path(path)
                prepared.append(item)
                if item.temporary:
                    temporary_paths.append(item.upload_path)

        expected_schema = prepared[0].schema
        for item in prepared[1:]:
            if not _schemas_equal(expected_schema, item.schema):
                raise DatasetError(
                    f"schema of {item.source_path} does not match the first input: "
                    f"got {item.schema}, want {expected_schema}"
                )
        yield prepared
    finally:
        for path in temporary_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _dataset_paths(data: object) -> list[Path] | None:
    if isinstance(data, (str, os.PathLike)):
        return [Path(data)]
    if isinstance(data, (Mapping, bytes, bytearray, memoryview)):
        return None
    # Tables and dataframes can also be iterable; do not consume their columns
    # (or a record batch reader's batches) while looking for file paths.
    if callable(getattr(data, "to_arrow", None)) or hasattr(data, "__dataframe__"):
        return None
    import pyarrow as pa

    if isinstance(data, (pa.Table, pa.RecordBatch, pa.RecordBatchReader)):
        return None
    if isinstance(data, Iterable):
        values = list(data)
        if not values or all(isinstance(value, (str, os.PathLike)) for value in values):
            return [Path(value) for value in values]
    return None


def _prepare_dataset_path(path: Path) -> _PreparedDatasetFile:
    if not path.exists():
        raise DatasetError(f"dataset file does not exist: {path}")
    if path.is_dir():
        raise DatasetError(f"{path} is a directory; pass individual files")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        upload_path, schema = _convert_csv_to_parquet(path)
        return _PreparedDatasetFile(
            source_path=path,
            upload_path=upload_path,
            size=upload_path.stat().st_size,
            schema=schema,
            temporary=True,
        )
    if suffix != ".parquet":
        raise DatasetError(
            f"unsupported dataset file {path}: expected .csv or .parquet"
        )
    schema = _read_parquet_schema(path)
    _validate_schema(schema, source=path)
    return _PreparedDatasetFile(
        source_path=path,
        upload_path=path,
        size=path.stat().st_size,
        schema=schema,
    )


def _read_parquet_schema(path: Path) -> Any:
    import pyarrow.parquet as pq

    try:
        return pq.ParquetFile(path).schema_arrow
    except Exception as exc:
        raise DatasetError(f"failed to read Parquet schema from {path}: {exc}") from exc


def _convert_csv_to_parquet(path: Path) -> tuple[Path, Any]:
    import pyarrow.csv as pa_csv
    import pyarrow.parquet as pq

    schema = _infer_csv_schema(path)
    _validate_schema(schema, source=path)
    output = _temporary_parquet_path()
    try:
        reader = pa_csv.open_csv(
            path,
            read_options=pa_csv.ReadOptions(block_size=_CSV_BLOCK_SIZE_BYTES),
            convert_options=pa_csv.ConvertOptions(
                column_types={field.name: field.type for field in schema},
                null_values=["", "NULL", "null"],
                strings_can_be_null=True,
            ),
        )
        writer = pq.ParquetWriter(output, schema, compression="snappy")
        try:
            for batch in reader:
                writer.write_batch(batch)
        finally:
            writer.close()
    except Exception as exc:
        try:
            output.unlink()
        except FileNotFoundError:
            pass
        raise DatasetError(f"failed to convert {path} to Parquet: {exc}") from exc
    return output, schema


def _infer_csv_schema(path: Path) -> Any:
    import pyarrow as pa

    try:
        file = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise DatasetError(f"failed to open CSV {path}: {exc}") from exc
    with file:
        reader = csv.reader(file)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise DatasetError(f"CSV is empty: {path}") from exc
        if not header:
            raise DatasetError(f"CSV has no columns: {path}")
        seen: set[str] = set()
        for name in header:
            if not name:
                raise DatasetError(f"CSV column names must not be empty: {path}")
            if name in seen:
                raise DatasetError(f"CSV column name {name!r} is duplicated: {path}")
            seen.add(name)

        kinds = [_CSV_KIND_UNKNOWN] * len(header)
        for row in reader:
            if len(row) != len(header):
                raise DatasetError(
                    f"CSV row {reader.line_num} in {path} has {len(row)} values; "
                    f"expected {len(header)}"
                )
            for index, value in enumerate(row):
                if value in ("", "NULL", "null"):
                    continue
                kinds[index] = _merge_csv_kinds(
                    kinds[index], _infer_csv_value_kind(value)
                )

    arrow_types = {
        _CSV_KIND_UNKNOWN: pa.string(),
        _CSV_KIND_INT64: pa.int64(),
        _CSV_KIND_FLOAT64: pa.float64(),
        _CSV_KIND_BOOL: pa.bool_(),
        _CSV_KIND_STRING: pa.string(),
    }
    return pa.schema(
        [
            pa.field(name, arrow_types[kind], nullable=True)
            for name, kind in zip(header, kinds, strict=True)
        ]
    )


_CSV_KIND_UNKNOWN = 0
_CSV_KIND_INT64 = 1
_CSV_KIND_FLOAT64 = 2
_CSV_KIND_BOOL = 3
_CSV_KIND_STRING = 4
_CSV_BOOL_VALUES = frozenset(
    {"1", "t", "T", "TRUE", "true", "True", "0", "f", "F", "FALSE", "false", "False"}
)


def _infer_csv_value_kind(value: str) -> int:
    try:
        parsed = int(value, 10)
        if -(1 << 63) <= parsed < (1 << 63):
            return _CSV_KIND_INT64
    except ValueError:
        pass
    try:
        float(value)
        return _CSV_KIND_FLOAT64
    except ValueError:
        pass
    if value in _CSV_BOOL_VALUES:
        return _CSV_KIND_BOOL
    return _CSV_KIND_STRING


def _merge_csv_kinds(left: int, right: int) -> int:
    if left == _CSV_KIND_UNKNOWN or left == right:
        return right
    if right == _CSV_KIND_UNKNOWN:
        return left
    if {left, right} == {_CSV_KIND_INT64, _CSV_KIND_FLOAT64}:
        return _CSV_KIND_FLOAT64
    return _CSV_KIND_STRING


def _write_tabular_data_to_parquet(data: object) -> tuple[Path, Any]:
    import pyarrow.parquet as pq

    table = _to_arrow_table(data)
    schema = table.schema
    _validate_schema(schema, source="in-memory data")
    output = _temporary_parquet_path()
    try:
        pq.write_table(table, output, compression="snappy")
    except Exception as exc:
        try:
            output.unlink()
        except FileNotFoundError:
            pass
        raise DatasetError(f"failed to encode dataset as Parquet: {exc}") from exc
    return output, schema


def _to_arrow_table(data: object) -> Any:
    import pyarrow as pa

    if isinstance(data, pa.Table):
        return data
    if isinstance(data, pa.RecordBatch):
        return pa.Table.from_batches([data])
    if isinstance(data, pa.RecordBatchReader):
        return data.read_all()
    if isinstance(data, Mapping):
        try:
            return pa.table(data)
        except Exception as exc:
            raise DatasetError(
                f"could not convert column data to Arrow: {exc}"
            ) from exc
    if isinstance(data, Sequence) and not isinstance(
        data, (str, bytes, bytearray, memoryview)
    ):
        rows = list(data)
        if all(isinstance(row, Mapping) for row in rows):
            try:
                return pa.Table.from_pylist(rows)
            except Exception as exc:
                raise DatasetError(
                    f"could not convert row data to Arrow: {exc}"
                ) from exc

    to_arrow = getattr(data, "to_arrow", None)
    if callable(to_arrow):
        try:
            converted = to_arrow()
        except Exception as exc:
            raise DatasetError(f"dataframe to_arrow() failed: {exc}") from exc
        if isinstance(converted, pa.RecordBatch):
            return pa.Table.from_batches([converted])
        if isinstance(converted, pa.Table):
            return converted

    if hasattr(data, "__dataframe__"):
        try:
            from pyarrow.interchange import from_dataframe

            return from_dataframe(data)
        except Exception as exc:
            raise DatasetError(
                f"could not convert dataframe-interchange input to Arrow: {exc}"
            ) from exc

    raise TypeError(
        "data must be CSV/Parquet path(s), a PyArrow table or record batch, "
        "column/row mappings, or a dataframe convertible to Arrow"
    )


def _validate_schema(schema: Any, *, source: object) -> None:
    if len(schema) == 0:
        raise DatasetError(f"dataset schema must contain at least one column: {source}")
    seen: set[str] = set()
    for field in schema:
        if not field.name:
            raise DatasetError(f"column names must not be empty: {source}")
        if field.name in seen:
            raise DatasetError(f"column name {field.name!r} is duplicated: {source}")
        seen.add(field.name)


def _schemas_equal(left: Any, right: Any) -> bool:
    if len(left) != len(right):
        return False
    return all(
        left_field.name == right_field.name
        and left_field.nullable == right_field.nullable
        and left_field.type.equals(right_field.type)
        for left_field, right_field in zip(left, right, strict=True)
    )


def _temporary_parquet_path() -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix="chalk-dataset-upload-", suffix=".parquet"
    )
    os.close(descriptor)
    return Path(name)


def _file_chunks(path: Path, *, offset: int, length: int) -> Iterable[bytes]:
    def chunks() -> Iterable[bytes]:
        with path.open("rb") as file:
            file.seek(offset)
            remaining = length
            while remaining:
                chunk = file.read(min(remaining, _TRANSFER_CHUNK_SIZE_BYTES))
                if not chunk:
                    raise DatasetError(
                        f"dataset file ended before the promised content length: {path}"
                    )
                remaining -= len(chunk)
                yield chunk

    return chunks()


def _append_query(uri: str, extra: str) -> str:
    return f"{uri}{'&' if '?' in uri else '?'}{extra}"


def _raise_for_http_status(response: HTTPResponse[bytes], *, operation: str) -> None:
    if 200 <= response.status < 300:
        return
    detail = response.content[:_MAX_ERROR_BODY_BYTES].decode("utf-8", errors="replace")
    suffix = f": {detail}" if detail else ""
    raise DatasetError(f"{operation} failed with HTTP {response.status}{suffix}")


def _http_status_for_code(code: Code) -> int | None:
    return {
        Code.INVALID_ARGUMENT: 400,
        Code.UNAUTHENTICATED: 401,
        Code.PERMISSION_DENIED: 403,
        Code.NOT_FOUND: 404,
        Code.ALREADY_EXISTS: 409,
        Code.RESOURCE_EXHAUSTED: 429,
        Code.INTERNAL: 500,
        Code.UNAVAILABLE: 503,
    }.get(code)
