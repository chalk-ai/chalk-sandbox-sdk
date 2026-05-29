from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import grpc
import pytest

from chalkcompute._gen.chalk.volume.v2 import volume_pb2 as pb_v2
from chalkcompute._volume import (
    VolumeClosedError,
    VolumeError,
)
from chalkcompute._vvolume import (
    _VERSIONED_INLINE_THRESHOLD_BYTES,
    VersionedVolume,
    VersionedVolumeClient,
    _read_versioned_file_response,
)


def _make_client() -> Any:
    client = object.__new__(VersionedVolumeClient)
    client._metadata = (("authorization", "Bearer test-token"),)
    client._stub = MagicMock()
    client.close = MagicMock()
    return client


class FakeRpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode) -> None:
        self._code = code

    def code(self) -> grpc.StatusCode:
        return self._code


def _volume(name: str = "models") -> pb_v2.VolumeInfo:
    return pb_v2.VolumeInfo(
        volume_id="vol-123",
        name=name,
        access_mode=pb_v2.VOLUME_ACCESS_MODE_READ_WRITE,
        ref="main",
    )


def _version(version_id: int = 10, sequence_number: int = 20) -> pb_v2.VersionInfo:
    return pb_v2.VersionInfo(
        version_id=version_id,
        sequence_number=sequence_number,
        ref="main",
        commit_id=f"commit-{version_id}",
    )


def _committed_status(
    name: str = "models", version_id: int = 11, sequence_number: int = 21
) -> pb_v2.CommitStatus:
    return pb_v2.CommitStatus(
        volume=pb_v2.VolumeRef(name=name, volume_id="vol-123"),
        commit_id="commit-1",
        result=pb_v2.COMMIT_RESULT_COMMITTED,
        version=_version(version_id, sequence_number),
    )


def _set_latest(client: Any, name: str = "models") -> None:
    client._stub.GetVolume.return_value = pb_v2.GetVolumeResponse(
        volume=_volume(name),
        version=_version(),
    )


def test_accepts_explicit_client_and_gets_existing_volume_lazily() -> None:
    client = _make_client()
    _set_latest(client)

    vol = VersionedVolume("models", client=client)

    assert vol.name == "models"
    assert vol._client is client
    client._stub.GetVolume.assert_not_called()
    client._stub.CreateVolume.assert_not_called()
    assert vol.info.name == "models"
    client._stub.GetVolume.assert_called_once()
    client._stub.CreateVolume.assert_not_called()


def test_accepts_name_only_and_creates_missing_volume_lazily() -> None:
    client = _make_client()
    client._stub.GetVolume.side_effect = FakeRpcError(grpc.StatusCode.NOT_FOUND)
    client._stub.CreateVolume.return_value = pb_v2.CreateVolumeResponse(
        volume=_volume("models"),
    )

    with patch.object(
        VersionedVolumeClient, "from_connect", return_value=client
    ) as from_connect:
        vol = VersionedVolume("models")

    assert vol.name == "models"
    assert vol._client is client
    from_connect.assert_called_once()
    client._stub.GetVolume.assert_not_called()
    client._stub.CreateVolume.assert_not_called()
    assert vol.info.name == "models"
    client._stub.GetVolume.assert_called_once()
    client._stub.CreateVolume.assert_called_once()


def test_put_file_lazily_creates_missing_volume() -> None:
    client = _make_client()
    client._stub.GetVolume.side_effect = [
        FakeRpcError(grpc.StatusCode.NOT_FOUND),
        pb_v2.GetVolumeResponse(volume=_volume("models"), version=_version()),
    ]
    client._stub.CreateVolume.return_value = pb_v2.CreateVolumeResponse(
        volume=_volume("models"),
    )
    client._stub.CommitVersion.return_value = pb_v2.CommitVersionResponse(
        status=_committed_status()
    )
    vol = VersionedVolume("models", client=client)

    vol.put_file("models/latest.bin", b"hello")

    client._stub.CreateVolume.assert_called_once()
    assert client._stub.CommitVersion.call_count == 1


def test_can_create_handle_without_get_or_create() -> None:
    client = _make_client()

    vol = VersionedVolume("models", client=client, create_if_missing=False)

    assert vol.name == "models"
    client._stub.GetVolume.assert_not_called()
    client._stub.CreateVolume.assert_not_called()


def test_close_closes_default_client_once() -> None:
    client = _make_client()

    with patch.object(VersionedVolumeClient, "from_connect", return_value=client):
        vol = VersionedVolume("models")

    vol.close()
    vol.close()

    client.close.assert_called_once()


def test_close_closes_explicit_client_once() -> None:
    client = _make_client()
    vol = VersionedVolume("models", client=client)

    vol.close()
    vol.close()

    client.close.assert_called_once()


def test_context_manager_closes_client() -> None:
    client = _make_client()

    with patch.object(VersionedVolumeClient, "from_connect", return_value=client):
        with VersionedVolume("models") as vol:
            assert vol.name == "models"

    client.close.assert_called_once()


def test_closed_volume_operations_raise() -> None:
    client = _make_client()
    vol = VersionedVolume("models", client=client)
    vol.close()

    with pytest.raises(VolumeClosedError):
        _ = vol.info
    with pytest.raises(VolumeClosedError):
        _ = vol.version
    with pytest.raises(VolumeClosedError):
        vol.reload()
    with pytest.raises(VolumeClosedError):
        vol.put_file("model.bin", b"new")
    with pytest.raises(VolumeClosedError):
        vol.read_file("model.bin")
    with pytest.raises(VolumeClosedError):
        vol.listdir()
    with pytest.raises(VolumeClosedError):
        vol.remove_file("model.bin")
    with pytest.raises(VolumeClosedError):
        vol.versions()
    with pytest.raises(VolumeClosedError):
        vol.latest_version()
    with pytest.raises(VolumeClosedError):
        vol.delete()
    with pytest.raises(VolumeClosedError):
        vol.at_version(7)
    with pytest.raises(VolumeClosedError):
        with vol.batch_upload():
            pass
    with pytest.raises(VolumeClosedError):
        with vol:
            pass

    client._stub.GetVolume.assert_not_called()
    client._stub.CommitVersion.assert_not_called()


def test_put_file_commits_inline_path_delta() -> None:
    client = _make_client()
    _set_latest(client)
    client._stub.CommitVersion.return_value = pb_v2.CommitVersionResponse(
        status=_committed_status()
    )
    vol = VersionedVolume("models", client=client)

    vol.put_file("models/latest.bin", b"hello")

    req = client._stub.CommitVersion.call_args[0][0]
    assert req.intent.volume.name == "models"
    assert req.intent.ref == "main"
    assert req.intent.base_version_id == 10
    assert req.intent.base_sequence_number == 20
    assert req.intent.author == "chalkcompute"
    upsert = req.intent.path_deltas.upserts[0]
    assert upsert.path == "models/latest.bin"
    assert upsert.mode == pb_v2.PATH_WRITE_MODE_UPSERT
    assert upsert.node.metadata.mode == 0o644
    assert upsert.node.file.content.inline.data == b"hello"
    assert upsert.node.file.content.inline.size == 5
    assert len(upsert.node.file.content.inline.hash) == 64


def test_batch_upload_commits_one_version() -> None:
    client = _make_client()
    _set_latest(client)
    client._stub.CommitVersion.return_value = pb_v2.CommitVersionResponse(
        status=_committed_status()
    )
    vol = VersionedVolume("models", client=client)

    with vol.batch_upload() as batch:
        batch.put("data/train.csv", "train")
        batch.put("data/eval.csv", b"eval")

    assert client._stub.CommitVersion.call_count == 1
    req = client._stub.CommitVersion.call_args[0][0]
    assert [u.path for u in req.intent.path_deltas.upserts] == [
        "data/train.csv",
        "data/eval.csv",
    ]


def test_batch_upload_microbatches_by_file_count() -> None:
    client = _make_client()
    _set_latest(client)
    client._stub.CommitVersion.return_value = pb_v2.CommitVersionResponse(
        status=_committed_status()
    )
    vol = VersionedVolume("models", client=client)

    with vol.batch_upload(max_files_per_commit=2) as batch:
        for index in range(5):
            batch.put(f"data/{index}.txt", f"value-{index}")

    assert client._stub.CommitVersion.call_count == 3
    committed_paths = [
        [u.path for u in call.args[0].intent.path_deltas.upserts]
        for call in client._stub.CommitVersion.call_args_list
    ]
    assert committed_paths == [
        ["data/0.txt", "data/1.txt"],
        ["data/2.txt", "data/3.txt"],
        ["data/4.txt"],
    ]


def test_batch_upload_rejects_invalid_microbatch_size() -> None:
    client = _make_client()
    vol = VersionedVolume("models", client=client)

    with pytest.raises(ValueError, match="max_files_per_commit"):
        with vol.batch_upload(max_files_per_commit=0):
            pass


def test_large_put_file_uploads_chunks_before_commit() -> None:
    client = _make_client()
    _set_latest(client)
    client._stub.RequestUploadURLs.return_value = pb_v2.RequestUploadURLsResponse(
        urls=[
            pb_v2.UploadURLItem(
                object_key="volumes/vol-123/chunks/aa/chunk",
                signed_upload_uri="https://storage/upload",
            )
        ]
    )
    client._stub.CommitVersion.return_value = pb_v2.CommitVersionResponse(
        status=_committed_status()
    )
    vol = VersionedVolume("models", client=client)
    data = b"x" * (_VERSIONED_INLINE_THRESHOLD_BYTES + 1)

    with patch("httpx.put") as mock_put:
        mock_put.return_value = MagicMock(status_code=200)
        vol.put_file("large.bin", data)

    upload_req = client._stub.RequestUploadURLs.call_args[0][0]
    assert upload_req.volume.name == "models"
    assert upload_req.objects[0].kind == pb_v2.UPLOADED_OBJECT_KIND_CHUNK
    assert upload_req.objects[0].content_size == len(data)
    mock_put.assert_called_once()
    commit_req = client._stub.CommitVersion.call_args[0][0]
    chunked = commit_req.intent.path_deltas.upserts[0].node.file.content.chunked
    assert chunked.size == len(data)
    assert chunked.chunks[0].object_key == "volumes/vol-123/chunks/aa/chunk"
    assert commit_req.intent.uploaded_object_references[0].object_key == (
        "volumes/vol-123/chunks/aa/chunk"
    )


def test_large_put_file_uploads_multiple_chunks_before_commit() -> None:
    client = _make_client()
    _set_latest(client)
    client._stub.RequestUploadURLs.return_value = pb_v2.RequestUploadURLsResponse(
        urls=[
            pb_v2.UploadURLItem(
                object_key="volumes/vol-123/chunks/aa/chunk-1",
                signed_upload_uri="https://storage/upload-1",
            ),
            pb_v2.UploadURLItem(
                object_key="volumes/vol-123/chunks/bb/chunk-2",
                signed_upload_uri="https://storage/upload-2",
            ),
            pb_v2.UploadURLItem(
                object_key="volumes/vol-123/chunks/cc/chunk-3",
                signed_upload_uri="https://storage/upload-3",
            ),
        ]
    )
    client._stub.CommitVersion.return_value = pb_v2.CommitVersionResponse(
        status=_committed_status()
    )
    vol = VersionedVolume("models", client=client)

    with (
        patch("chalkcompute._vvolume._VERSIONED_INLINE_THRESHOLD_BYTES", 1),
        patch("chalkcompute._vvolume._VERSIONED_CHUNK_SIZE_BYTES", 4),
        patch("httpx.put") as mock_put,
    ):
        mock_put.return_value = MagicMock(status_code=200)
        vol.put_file("large.bin", b"hello world")

    assert mock_put.call_count == 3
    commit_req = client._stub.CommitVersion.call_args[0][0]
    chunks = commit_req.intent.path_deltas.upserts[0].node.file.content.chunked.chunks
    assert [chunk.offset for chunk in chunks] == [0, 4, 8]
    assert [chunk.size for chunk in chunks] == [4, 4, 3]


def test_chunked_read_downloads_chunks_and_joins_by_offset() -> None:
    resp = pb_v2.GetFileResponse(
        chunked=pb_v2.ChunkedFileContent(
            chunks=[
                pb_v2.SignedChunkRef(
                    signed_download_uri="https://storage/chunk-2",
                    offset=6,
                    size=5,
                ),
                pb_v2.SignedChunkRef(
                    signed_download_uri="https://storage/chunk-1",
                    offset=0,
                    size=6,
                ),
            ]
        )
    )

    def read(uri: str) -> bytes:
        return {
            "https://storage/chunk-1": b"hello ",
            "https://storage/chunk-2": b"world",
        }[uri]

    with patch("chalkcompute._vvolume._read_file_from_signed_uri", side_effect=read):
        assert _read_versioned_file_response(resp) == b"hello world"


def test_at_version_reads_with_version_selector() -> None:
    client = _make_client()
    _set_latest(client)
    client._stub.GetFile.return_value = pb_v2.GetFileResponse(data=b"old")
    vol = VersionedVolume("models", client=client).at_version(7)

    assert vol.read_file("model.bin") == b"old"

    req = client._stub.GetFile.call_args[0][0]
    assert req.volume.name == "models"
    assert req.selector.version_id == 7


def test_at_version_zero_pins_to_initial_version() -> None:
    client = _make_client()
    _set_latest(client)
    client._stub.GetFile.return_value = pb_v2.GetFileResponse(data=b"")
    vol = VersionedVolume("models", client=client).at_version(0)

    assert vol.read_file("model.bin") == b""

    req = client._stub.GetFile.call_args[0][0]
    assert req.selector.WhichOneof("selector") == "version_id"
    assert req.selector.version_id == 0


def test_at_version_is_read_only() -> None:
    client = _make_client()
    vol = VersionedVolume("models", client=client).at_version(7)

    with pytest.raises(VolumeError, match="read-only"):
        vol.put_file("model.bin", b"new")

    client._stub.CommitVersion.assert_not_called()
