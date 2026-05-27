from __future__ import annotations

import os
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import grpc
import httpx
import pytest

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.volume.v1 import volume_pb2 as pb
from chalkcompute._volume import (
    Volume,
    VolumeClosedError,
    VolumeClient,
    _INLINE_THRESHOLD_BYTES,
    _PRESIGNED_PART_SIZE_BYTES,
    _get_source_content_length,
    _read_file_bytes,
    _put_file_presigned,
    _upload_bytes,
    _upload_from_url,
    upload_lazy_files,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockConnect(ConnectClient):
    def __init__(self, api_server: str) -> None:
        self._api_server = api_server
        self._access_token = "token-123"
        self._env_id = "env-456"
        self._headers = None

    def _ensure_auth(self) -> None:
        pass


class FakeRpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode) -> None:
        self._code = code

    def code(self) -> grpc.StatusCode:
        return self._code


def _make_vol_client() -> Any:
    """Build a VolumeClient whose gRPC stubs are mocked out."""
    client = object.__new__(VolumeClient)
    client._metadata = [("authorization", "Bearer test-token")]
    client._stub = MagicMock()
    client._obj_stub = MagicMock()
    client._refresh = MagicMock()
    return client


def _set_resumable_response(
    client: VolumeClient,
    storage_object_id: str = "obj-gcs-123",
    signed_uri: str = "https://storage.googleapis.com/signed",
) -> None:
    resp = MagicMock()
    resp.storage_object_id = storage_object_id
    resp.WhichOneof.return_value = "resumable"
    resp.resumable.signed_upload_uri = signed_uri
    client._obj_stub.GetObjectUploadUri.return_value = resp


def _set_multipart_response(
    client: VolumeClient,
    storage_object_id: str = "obj-s3-456",
    part_uris: list[str] | None = None,
    completion_uri: str = "https://s3.amazonaws.com/complete",
) -> None:
    if part_uris is None:
        part_uris = ["https://s3.amazonaws.com/part/1"]
    resp = MagicMock()
    resp.storage_object_id = storage_object_id
    resp.WhichOneof.return_value = "multipart"
    resp.multipart.signed_upload_uris = part_uris + [completion_uri]
    client._obj_stub.GetObjectUploadUri.return_value = resp


# ---------------------------------------------------------------------------
# Volume constructor
# ---------------------------------------------------------------------------


class TestVolumeConstructor:
    def test_accepts_explicit_client_and_gets_existing_volume(self):
        client = _make_vol_client()
        client._stub.GetVolume.return_value = pb.GetVolumeResponse(
            volume=pb.VolumeInfo(name="my-data"),
        )

        vol = Volume("my-data", client=client)

        assert vol.name == "my-data"
        assert vol._client is client
        client._stub.GetVolume.assert_not_called()
        client._stub.CreateVolume.assert_not_called()
        assert vol.info.name == "my-data"
        client._stub.GetVolume.assert_called_once()
        client._stub.CreateVolume.assert_not_called()

    def test_accepts_name_only_and_creates_missing_volume(self):
        client = _make_vol_client()
        client._stub.GetVolume.side_effect = FakeRpcError(grpc.StatusCode.NOT_FOUND)
        client._stub.CreateVolume.return_value = pb.CreateVolumeResponse(
            volume=pb.VolumeInfo(name="my-data"),
        )

        with patch.object(VolumeClient, "from_connect", return_value=client) as from_connect:
            vol = Volume("my-data")

        assert vol.name == "my-data"
        assert vol._client is client
        from_connect.assert_called_once()
        client._stub.GetVolume.assert_not_called()
        client._stub.CreateVolume.assert_not_called()
        assert vol.info.name == "my-data"
        client._stub.CreateVolume.assert_called_once()

    def test_put_file_lazily_creates_missing_volume(self):
        client = _make_vol_client()
        client._stub.GetVolume.side_effect = FakeRpcError(grpc.StatusCode.NOT_FOUND)
        client._stub.CreateVolume.return_value = pb.CreateVolumeResponse(
            volume=pb.VolumeInfo(name="my-data"),
        )
        vol = Volume("my-data", client=client)

        vol.put_file("hello.txt", b"hello")

        client._stub.CreateVolume.assert_called_once()
        req = client._stub.PutFile.call_args[0][0]
        assert req.volume_name == "my-data"
        assert req.path == "hello.txt"
        assert req.data == b"hello"

    def test_can_create_handle_without_get_or_create(self):
        client = _make_vol_client()

        vol = Volume("my-data", client=client, create_if_missing=False)

        assert vol.name == "my-data"
        client._stub.GetVolume.assert_not_called()
        client._stub.CreateVolume.assert_not_called()

    def test_close_closes_default_client(self):
        client = _make_vol_client()
        client.close = MagicMock()

        with patch.object(VolumeClient, "from_connect", return_value=client):
            vol = Volume("my-data")

        vol.close()
        vol.close()

        client.close.assert_called_once()

    def test_close_closes_explicit_client(self):
        client = _make_vol_client()
        client.close = MagicMock()
        vol = Volume("my-data", client=client)

        vol.close()

        client.close.assert_called_once()

    def test_context_manager_closes_owned_client(self):
        client = _make_vol_client()
        client.close = MagicMock()

        with patch.object(VolumeClient, "from_connect", return_value=client):
            with Volume("my-data") as vol:
                assert vol.name == "my-data"

        client.close.assert_called_once()

    def test_closed_volume_operations_raise(self):
        client = _make_vol_client()
        client.close = MagicMock()
        vol = Volume("my-data", client=client)
        vol.close()

        with pytest.raises(VolumeClosedError):
            _ = vol.info
        with pytest.raises(VolumeClosedError):
            vol.reload()
        with pytest.raises(VolumeClosedError):
            vol.put_file("hello.txt", b"hello")
        with pytest.raises(VolumeClosedError):
            vol.read_file("hello.txt")
        with pytest.raises(VolumeClosedError):
            vol.listdir()
        with pytest.raises(VolumeClosedError):
            vol.remove_file("hello.txt")
        with pytest.raises(VolumeClosedError):
            vol.delete()
        with pytest.raises(VolumeClosedError):
            with vol.batch_upload():
                pass
        with pytest.raises(VolumeClosedError):
            with vol:
                pass

        client._stub.GetVolume.assert_not_called()
        client._stub.PutFile.assert_not_called()


# ---------------------------------------------------------------------------
# VolumeClient construction
# ---------------------------------------------------------------------------


class TestVolumeClientFromConnect:
    def test_builds_tls_client_from_connect_auth(self):
        connect = MockConnect("https://api.chalk.ai")

        with patch("chalkcompute._volume.ConnectClient", return_value=connect):
            with patch.object(connect, "_ensure_auth") as ensure_auth:
                client = VolumeClient.from_connect(connect)

        assert client._target == "api.chalk.ai:443"
        assert client._metadata == (
            ("authorization", "Bearer token-123"),
            ("x-chalk-env-id", "env-456"),
            ("x-chalk-server", "go-api"),
        )
        ensure_auth.assert_called_once_with()
        client.close()

    def test_preserves_explicit_port(self):
        connect = MockConnect("https://api.chalk.ai:7443")

        with patch.object(connect, "_ensure_auth"):
            client = VolumeClient.from_connect(connect)

        assert client._target == "api.chalk.ai:7443"
        client.close()

    def test_from_connect_called_by_volume_init_default(self):
        client = _make_vol_client()
        client._stub.GetVolume.return_value = pb.GetVolumeResponse(
            volume=pb.VolumeInfo(name="test-vol"),
        )

        with patch.object(VolumeClient, "from_connect", return_value=client) as from_connect:
            vol = Volume("test-vol")

        assert vol._client is client
        from_connect.assert_called_once()

    def test_channel_options_are_forwarded(self):
        client = VolumeClient(
            "localhost:50051",
            credentials=grpc.ssl_channel_credentials(),
            options=[
                ("grpc.max_send_message_length", -1),
                ("grpc.max_receive_message_length", -1),
            ],
        )

        assert client._target == "localhost:50051"
        client.close()


# ---------------------------------------------------------------------------
# Upload routing
# ---------------------------------------------------------------------------


class TestReadRouting:
    def test_signed_download_uri_uses_httpx(self):
        client = _make_vol_client()
        resp = MagicMock()
        resp.WhichOneof.return_value = "signed_download_uri"
        resp.signed_download_uri = "https://storage.example.com/file"
        client._stub.GetFile.return_value = resp

        with patch("httpx.get") as mock_get:
            mock_get.return_value = MagicMock(content=b"payload")
            data = _read_file_bytes(client, "vol", "file.txt")

        assert data == b"payload"
        req = client._stub.GetFile.call_args[0][0]
        assert req.volume_name == "vol"
        assert req.path == "file.txt"
        assert req.get_signed_uri is True
        mock_get.assert_called_once_with("https://storage.example.com/file", timeout=300)

    def test_inline_data_falls_back_without_http(self):
        client = _make_vol_client()
        resp = MagicMock()
        resp.WhichOneof.return_value = "data"
        resp.data = b"inline"
        client._stub.GetFile.return_value = resp

        with patch("httpx.get") as mock_get:
            data = _read_file_bytes(client, "vol", "file.txt")

        assert data == b"inline"
        mock_get.assert_not_called()


class TestUploadRouting:
    def test_small_file_routes_to_inline(self):
        client = _make_vol_client()

        _upload_bytes(client, "vol", "file.txt", b"x" * 100)

        req = client._stub.PutFile.call_args[0][0]
        assert req.data == b"x" * 100
        assert req.volume_name == "vol"
        assert req.path == "file.txt"
        client._obj_stub.GetObjectUploadUri.assert_not_called()

    def test_exactly_at_threshold_uses_inline(self):
        client = _make_vol_client()

        _upload_bytes(client, "vol", "exact.bin", b"y" * _INLINE_THRESHOLD_BYTES)

        client._stub.PutFile.assert_called_once()
        client._obj_stub.GetObjectUploadUri.assert_not_called()

    def test_above_threshold_uses_presigned(self):
        client = _make_vol_client()
        _set_resumable_response(client)
        data = b"z" * (_INLINE_THRESHOLD_BYTES + 1)

        with patch("httpx.post") as mock_post, patch("httpx.put") as mock_put:
            mock_post.return_value = MagicMock(
                status_code=200, is_success=True,
                headers={"Location": "https://session/"},
            )
            mock_put.return_value = MagicMock(status_code=200, is_success=True)

            _upload_bytes(client, "vol", "large.bin", data)

        client._obj_stub.GetObjectUploadUri.assert_called_once()
        req = client._stub.PutFile.call_args[0][0]
        assert req.storage_object_id == "obj-gcs-123"
        assert not req.data

    def test_inline_upload_logs_completion(self, caplog):
        client = _make_vol_client()

        with caplog.at_level(logging.INFO, logger="chalkcompute._volume"):
            _upload_bytes(client, "vol", "file.txt", b"x" * 100)

        assert "Uploaded file.txt (inline, 100 bytes)" in caplog.text


# ---------------------------------------------------------------------------
# Presigned upload: GCS resumable
# ---------------------------------------------------------------------------


class TestPresignedResumable:
    def test_success(self):
        client = _make_vol_client()
        _set_resumable_response(client, "gcs-obj", "https://gcs/signed")
        data = b"B" * (_INLINE_THRESHOLD_BYTES + 1)

        with patch("httpx.post") as mock_post, patch("httpx.put") as mock_put:
            mock_post.return_value = MagicMock(
                status_code=200, is_success=True,
                headers={"Location": "https://session/"},
            )
            mock_put.return_value = MagicMock(status_code=200, is_success=True)

            _put_file_presigned(client, "vol", "data.bin", data)

        assert mock_post.call_args[0][0] == "https://gcs/signed"
        assert mock_put.call_args[0][0] == "https://session/"
        assert client._stub.PutFile.call_args[0][0].storage_object_id == "gcs-obj"

    def test_refreshes_on_401(self):
        client = _make_vol_client()
        first = MagicMock(storage_object_id="old")
        first.WhichOneof.return_value = "resumable"
        first.resumable.signed_upload_uri = "https://gcs/old"
        second = MagicMock(storage_object_id="new")
        second.WhichOneof.return_value = "resumable"
        second.resumable.signed_upload_uri = "https://gcs/new"
        client._obj_stub.GetObjectUploadUri.side_effect = [first, second]

        data = b"C" * (_INLINE_THRESHOLD_BYTES + 1)

        with patch("httpx.post") as mock_post, patch("httpx.put") as mock_put:
            mock_post.side_effect = [
                MagicMock(status_code=401, is_success=False),
                MagicMock(status_code=200, is_success=True,
                          headers={"Location": "https://session/"}),
            ]
            mock_put.return_value = MagicMock(status_code=200, is_success=True)

            _put_file_presigned(client, "vol", "file.bin", data)

        assert client._obj_stub.GetObjectUploadUri.call_count == 2
        assert client._stub.PutFile.call_args[0][0].storage_object_id == "new"

    def test_logs_completion(self, caplog):
        client = _make_vol_client()
        _set_resumable_response(client, "gcs-obj", "https://gcs/signed")
        data = b"B" * (_INLINE_THRESHOLD_BYTES + 1)

        with patch("httpx.post") as mock_post, patch("httpx.put") as mock_put:
            mock_post.return_value = MagicMock(
                status_code=200, is_success=True,
                headers={"Location": "https://session/"},
            )
            mock_put.return_value = MagicMock(status_code=200, is_success=True)

            with caplog.at_level(logging.INFO, logger="chalkcompute._volume"):
                _put_file_presigned(client, "vol", "data.bin", data)

        assert "Resumable upload complete: object=gcs-obj refreshes=0" in caplog.text
        assert "Uploaded data.bin (presigned resumable" in caplog.text


# ---------------------------------------------------------------------------
# Presigned upload: S3 multipart
# ---------------------------------------------------------------------------


class TestPresignedMultipart:
    def test_success(self):
        client = _make_vol_client()
        _set_multipart_response(client, "s3-obj",
                                part_uris=["https://s3/part/1"],
                                completion_uri="https://s3/complete")
        data = b"D" * (_INLINE_THRESHOLD_BYTES + 1)

        with patch("httpx.put") as mock_put, patch("httpx.post") as mock_post:
            mock_put.return_value = MagicMock(
                status_code=200, is_success=True, headers={"ETag": '"abc"'},
            )
            mock_post.return_value = MagicMock(status_code=200, is_success=True)

            _put_file_presigned(client, "vol", "archive.tar", data)

        mock_put.assert_called_once_with("https://s3/part/1", content=data, timeout=300)
        assert mock_post.call_args[0][0] == "https://s3/complete"
        assert b"abc" in mock_post.call_args[1]["content"]
        assert client._stub.PutFile.call_args[0][0].storage_object_id == "s3-obj"

    def test_refreshes_on_403(self):
        import httpx as _httpx

        client = _make_vol_client()
        first = MagicMock(storage_object_id="old")
        first.WhichOneof.return_value = "multipart"
        first.multipart.signed_upload_uris = ["https://s3/p-old", "https://s3/c-old"]
        second = MagicMock(storage_object_id="new")
        second.WhichOneof.return_value = "multipart"
        second.multipart.signed_upload_uris = ["https://s3/p-new", "https://s3/c-new"]
        client._obj_stub.GetObjectUploadUri.side_effect = [first, second]

        data = b"E" * (_INLINE_THRESHOLD_BYTES + 1)

        forbidden = MagicMock(status_code=403, is_success=False)
        forbidden.raise_for_status.side_effect = _httpx.HTTPStatusError(
            "403", request=MagicMock(), response=forbidden,
        )

        with patch("httpx.put") as mock_put, patch("httpx.post") as mock_post:
            mock_put.side_effect = [
                forbidden,
                MagicMock(status_code=200, is_success=True, headers={"ETag": '"e1"'}),
            ]
            mock_post.return_value = MagicMock(status_code=200, is_success=True)

            _put_file_presigned(client, "vol", "data.bin", data)

        assert client._obj_stub.GetObjectUploadUri.call_count == 2
        assert client._stub.PutFile.call_args[0][0].storage_object_id == "new"

    def test_logs_completion(self, caplog):
        client = _make_vol_client()
        _set_multipart_response(
            client,
            "s3-obj",
            part_uris=["https://s3/part/1"],
            completion_uri="https://s3/complete",
        )
        data = b"D" * (_INLINE_THRESHOLD_BYTES + 1)

        with patch("httpx.put") as mock_put, patch("httpx.post") as mock_post:
            mock_put.return_value = MagicMock(
                status_code=200, is_success=True, headers={"ETag": '"abc"'},
            )
            mock_post.return_value = MagicMock(status_code=200, is_success=True)

            with caplog.at_level(logging.INFO, logger="chalkcompute._volume"):
                _put_file_presigned(client, "vol", "archive.tar", data)

        assert "Multipart upload complete: object=s3-obj parts=1 refreshes=0" in caplog.text
        assert "Uploaded archive.tar (presigned multipart" in caplog.text


# ---------------------------------------------------------------------------
# upload_lazy_files
# ---------------------------------------------------------------------------


class TestUploadLazyFiles:
    def test_creates_volume_and_uploads(self, tmp_path: Path):
        src = tmp_path / "hello.txt"
        src.write_bytes(b"hello")

        from chalkcompute._image import Image, LazyLocalFile

        image = Image.base("python:3.14")
        image._lazy_local_files = {LazyLocalFile(src=str(src), dest="/app/hello.txt")}

        client = _make_vol_client()
        mock_vol = MagicMock()
        mock_vol.name = "test-vol-abc"
        client.create = MagicMock(return_value=mock_vol)

        with patch("chalkcompute._volume._upload_bytes") as mock_upload:
            volumes = upload_lazy_files(client, image)

        client.create.assert_called_once()
        mock_upload.assert_called_once()
        assert mock_upload.call_args[0][1] == "test-vol-abc"
        assert mock_upload.call_args[0][3] == b"hello"
        assert volumes == [("test-vol-abc", "/app")]

    def test_empty_image_returns_early(self):
        from chalkcompute._image import Image

        image = Image.base("python:3.14")
        image._lazy_local_files = set()

        client = _make_vol_client()
        client.create = MagicMock()

        assert upload_lazy_files(client, image) == []
        client.create.assert_not_called()


# ---------------------------------------------------------------------------
# Streaming URL upload
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    """Minimal mock for httpx streaming response context manager."""

    def __init__(self, data: bytes, status_code: int = 200, content_length: int | None = None):
        self._data = data
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        cl = content_length if content_length is not None else len(data)
        self.headers = {"content-length": str(cl)}

    def raise_for_status(self) -> None:
        if not self.is_success:
            raise httpx.HTTPStatusError("error", request=MagicMock(), response=self)  # type: ignore[arg-type]

    def iter_bytes(self, chunk_size: int = 65536) -> Iterator[bytes]:
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i : i + chunk_size]

    def __enter__(self) -> _FakeStreamResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def close(self) -> None:
        pass


class TestGetSourceContentLength:
    def test_head_success(self):
        with patch("httpx.head") as mock_head:
            mock_head.return_value = MagicMock(
                is_success=True,
                headers={"content-length": "12345"},
            )
            assert _get_source_content_length("https://example.com/file") == 12345

    def test_head_fails_falls_back_to_get(self):
        with patch("httpx.head") as mock_head, patch("httpx.stream") as mock_stream:
            mock_head.side_effect = httpx.HTTPError("HEAD not allowed")
            mock_stream.return_value = _FakeStreamResponse(b"x" * 100, content_length=100)
            assert _get_source_content_length("https://example.com/file") == 100

    def test_head_no_content_length_falls_back(self):
        with patch("httpx.head") as mock_head, patch("httpx.stream") as mock_stream:
            mock_head.return_value = MagicMock(is_success=True, headers={})
            mock_stream.return_value = _FakeStreamResponse(b"x" * 200, content_length=200)
            assert _get_source_content_length("https://example.com/file") == 200


class TestUploadFromUrlSmallFile:
    def test_small_file_uses_inline(self):
        client = _make_vol_client()
        data = b"small" * 10  # well under 2MB

        with patch("chalkcompute._volume._get_source_content_length", return_value=len(data)), \
             patch("httpx.get") as mock_get:
            mock_get.return_value = MagicMock(content=data)
            _upload_from_url(client, "vol", "model.bin", "https://example.com/model")

        client._stub.PutFile.assert_called_once()
        req = client._stub.PutFile.call_args[0][0]
        assert req.data == data
        assert req.volume_name == "vol"
        assert req.path == "model.bin"


class TestUploadFromUrlMultipart:
    def test_streaming_multipart_success(self):
        client = _make_vol_client()
        part_size = _PRESIGNED_PART_SIZE_BYTES
        data = b"A" * (part_size + 100)  # slightly more than one part

        _set_multipart_response(
            client, "s3-stream-obj",
            part_uris=["https://s3/part/1", "https://s3/part/2"],
            completion_uri="https://s3/complete",
        )

        with patch("chalkcompute._volume._get_source_content_length", return_value=len(data)), \
             patch("httpx.stream") as mock_stream, \
             patch("httpx.put") as mock_put, \
             patch("httpx.post") as mock_post:

            mock_stream.return_value = _FakeStreamResponse(data)
            mock_put.return_value = MagicMock(
                status_code=200, is_success=True, headers={"ETag": '"e1"'},
            )
            mock_post.return_value = MagicMock(status_code=200, is_success=True)

            _upload_from_url(client, "vol", "big.bin", "https://example.com/big")

        assert mock_put.call_count == 2
        assert mock_put.call_args_list[0][1]["content"] == data[:part_size]
        assert mock_put.call_args_list[1][1]["content"] == data[part_size:]

        assert mock_post.call_args[0][0] == "https://s3/complete"
        assert b"e1" in mock_post.call_args[1]["content"]

        req = client._stub.PutFile.call_args[0][0]
        assert req.storage_object_id == "s3-stream-obj"

    def test_multipart_retry_on_403(self):
        import httpx as _httpx

        client = _make_vol_client()
        part_size = _PRESIGNED_PART_SIZE_BYTES
        data = b"B" * (part_size + 50)

        first = MagicMock(storage_object_id="old")
        first.WhichOneof.return_value = "multipart"
        first.multipart.signed_upload_uris = ["https://s3/p-old/1", "https://s3/p-old/2", "https://s3/c-old"]

        second = MagicMock(storage_object_id="new")
        second.WhichOneof.return_value = "multipart"
        second.multipart.signed_upload_uris = ["https://s3/p-new/1", "https://s3/p-new/2", "https://s3/c-new"]

        client._obj_stub.GetObjectUploadUri.side_effect = [first, second]

        forbidden = MagicMock(status_code=403, is_success=False)
        forbidden.raise_for_status.side_effect = _httpx.HTTPStatusError(
            "403", request=MagicMock(), response=forbidden,
        )

        ok_resp = MagicMock(status_code=200, is_success=True, headers={"ETag": '"ok"'})

        call_count = 0

        def put_side_effect(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _httpx.HTTPStatusError("403", request=MagicMock(), response=forbidden)
            return ok_resp

        with patch("chalkcompute._volume._get_source_content_length", return_value=len(data)), \
             patch("httpx.stream") as mock_stream, \
             patch("httpx.put", side_effect=put_side_effect), \
             patch("httpx.post") as mock_post:

            mock_stream.return_value = _FakeStreamResponse(data)
            mock_post.return_value = MagicMock(status_code=200, is_success=True)

            _upload_from_url(client, "vol", "retry.bin", "https://example.com/retry")

        assert client._obj_stub.GetObjectUploadUri.call_count == 2
        req = client._stub.PutFile.call_args[0][0]
        assert req.storage_object_id == "new"


class TestUploadFromUrlResumable:
    def test_streaming_resumable_success(self):
        client = _make_vol_client()
        part_size = _PRESIGNED_PART_SIZE_BYTES
        data = b"C" * (part_size + 200)

        _set_resumable_response(client, "gcs-stream-obj", "https://gcs/signed")

        init_resp = MagicMock(
            is_success=True, status_code=200,
            headers={"Location": "https://gcs/session/123"},
        )
        chunk_308 = MagicMock(status_code=308, is_success=True)
        chunk_200 = MagicMock(status_code=200, is_success=True)

        with patch("chalkcompute._volume._get_source_content_length", return_value=len(data)), \
             patch("httpx.stream") as mock_stream, \
             patch("httpx.post") as mock_post, \
             patch("httpx.put") as mock_put:

            mock_stream.return_value = _FakeStreamResponse(data)
            mock_post.return_value = init_resp
            mock_put.side_effect = [chunk_308, chunk_200]

            _upload_from_url(client, "vol", "gcs.bin", "https://example.com/gcs")

        mock_post.assert_called_once()
        assert mock_put.call_count == 2

        first_put = mock_put.call_args_list[0]
        assert f"bytes 0-{part_size - 1}/{len(data)}" in first_put[1]["headers"]["Content-Range"]

        second_put = mock_put.call_args_list[1]
        assert f"bytes {part_size}-{len(data) - 1}/{len(data)}" in second_put[1]["headers"]["Content-Range"]

        req = client._stub.PutFile.call_args[0][0]
        assert req.storage_object_id == "gcs-stream-obj"


class TestVolumePutFileFromUrl:
    def test_method_delegates_and_chains(self):
        client = _make_vol_client()
        client._stub.GetVolume.return_value = pb.GetVolumeResponse(
            volume=pb.VolumeInfo(name="test-vol")
        )
        vol = Volume("test-vol", client=client)

        with patch("chalkcompute._volume._upload_from_url") as mock_upload:
            result = vol.put_file_from_url("model.bin", "https://example.com/model")

        mock_upload.assert_called_once_with(client, "test-vol", "model.bin", "https://example.com/model")
        assert result is vol

    def test_closed_volume_raises(self):
        client = _make_vol_client()
        client.close = MagicMock()
        vol = Volume("test-vol", client=client)
        vol.close()

        with pytest.raises(VolumeClosedError):
            vol.put_file_from_url("model.bin", "https://example.com/model")
