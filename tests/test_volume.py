from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import grpc
import pytest

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.volume.v1 import volume_pb2 as pb
from chalkcompute._volume import (
    SimpleVolume as Volume,
    VolumeClosedError,
    VolumeClient,
    _INLINE_THRESHOLD_BYTES,
    _read_file_bytes,
    _put_file_presigned,
    _upload_bytes,
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

        with patch.object(
            VolumeClient, "from_connect", return_value=client
        ) as from_connect:
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

        with patch.object(
            VolumeClient, "from_connect", return_value=client
        ) as from_connect:
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
        mock_get.assert_called_once_with(
            "https://storage.example.com/file", timeout=300
        )

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
                status_code=200,
                is_success=True,
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
                status_code=200,
                is_success=True,
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
                MagicMock(
                    status_code=200,
                    is_success=True,
                    headers={"Location": "https://session/"},
                ),
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
                status_code=200,
                is_success=True,
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
        _set_multipart_response(
            client,
            "s3-obj",
            part_uris=["https://s3/part/1"],
            completion_uri="https://s3/complete",
        )
        data = b"D" * (_INLINE_THRESHOLD_BYTES + 1)

        with patch("httpx.put") as mock_put, patch("httpx.post") as mock_post:
            mock_put.return_value = MagicMock(
                status_code=200,
                is_success=True,
                headers={"ETag": '"abc"'},
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
            "403",
            request=MagicMock(),
            response=forbidden,
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
                status_code=200,
                is_success=True,
                headers={"ETag": '"abc"'},
            )
            mock_post.return_value = MagicMock(status_code=200, is_success=True)

            with caplog.at_level(logging.INFO, logger="chalkcompute._volume"):
                _put_file_presigned(client, "vol", "archive.tar", data)

        assert (
            "Multipart upload complete: object=s3-obj parts=1 refreshes=0"
            in caplog.text
        )
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
            volumes = client.upload_lazy_files(image)

        client.create.assert_called_once()
        mock_upload.assert_called_once()
        assert mock_upload.call_args[0][1] == "test-vol-abc"
        assert mock_upload.call_args[0][3] == b"hello"
        from chalkcompute._volume import VolumeMount

        assert volumes == [
            VolumeMount(name="test-vol-abc", mount_path="/app", type="chalkfs")
        ]

    def test_empty_image_returns_early(self):
        from chalkcompute._image import Image

        image = Image.base("python:3.14")
        image._lazy_local_files = set()

        client = _make_vol_client()
        client.create = MagicMock()

        assert client.upload_lazy_files(image) == []
        client.create.assert_not_called()
