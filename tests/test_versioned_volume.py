from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from chalkcompute._gen.chalk.volume.v2 import volume_pb2 as pb_v2
from chalkcompute._volume import VolumeClosedError, VolumeError, VolumeNotFoundError
from chalkcompute import _vvolume as vv
from chalkcompute._vvolume import (
    CommitStatus,
    VersionInfo,
    VersionedVolume,
    VersionedVolumeClient,
    _VERSIONED_CHUNK_MAX_RETRIES,
    _VERSIONED_CHUNK_CONCURRENCY,
    _VERSIONED_CHUNK_SIZE_BYTES,
    _VERSIONED_COMMIT_MAX_REBASE_RETRIES,
    _VERSIONED_FILE_CONCURRENCY,
    _VERSIONED_INLINE_THRESHOLD_BYTES,
    _VERSIONED_MAX_PACK_BYTES,
    _VERSIONED_RATE_LIMIT_MAX_RETRIES,
    _read_versioned_file_response,
)


@pytest.fixture(autouse=True)
def _disable_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHALK_REMOTE_FUNCTION_TRACE_POLICY", "always_off")


def _volume_dict(name: str = "models") -> dict[str, Any]:
    return {
        "volume_id": "vol-123",
        "name": name,
        "access_mode": 2,
        "ref": "main",
    }


def _version_dict(version_id: int = 10, sequence_number: int = 20) -> dict[str, Any]:
    return {
        "version_id": version_id,
        "sequence_number": sequence_number,
        "ref": "main",
        "commit_id": f"commit-{version_id}",
    }


def _status_dict(version_id: int = 11, sequence_number: int = 21) -> dict[str, Any]:
    return {
        "commit_id": "commit-1",
        "result": "committed",
        "version": _version_dict(version_id, sequence_number),
    }


def _make_client(monkeypatch: pytest.MonkeyPatch) -> VersionedVolumeClient:
    monkeypatch.setattr(vv, "_NativeVolumeRpcError", RuntimeError)
    client = object.__new__(VersionedVolumeClient)
    client._closed = False
    client._inner = MagicMock()
    client.close = MagicMock()
    return client


def _not_found() -> RuntimeError:
    return RuntimeError("not_found", "missing")


def test_client_from_connect_uses_connect_http_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = MagicMock()
    monkeypatch.setattr(vv, "_NativeVersionedVolumeClient", native)
    connect = MagicMock()
    connect._api_server = "https://api.chalk.ai/"
    connect._access_token = "token"
    connect._env_id = "env-1"

    client = VersionedVolumeClient.from_connect(connect)

    connect._ensure_auth.assert_called_once()
    assert client._endpoint == "https://api.chalk.ai"
    # commit_author is None so the native client derives the agent URN
    # from the credentials.
    native.assert_called_once_with(
        "https://api.chalk.ai",
        "token",
        "env-1",
        None,
        [],
        "go-api",
    )


def test_accepts_explicit_client_and_gets_existing_volume_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(),
    }

    vol = VersionedVolume("models", client=client)

    assert vol.name == "models"
    assert vol._client is client
    client._inner.get_volume.assert_not_called()
    assert vol.info.name == "models"
    assert vol.version.id == 10
    client._inner.get_volume.assert_called_once_with("models", None, None)


def test_accepts_name_only_and_creates_missing_volume_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.side_effect = _not_found()
    client._inner.create_volume.return_value = {"volume": _volume_dict()}

    with patch.object(
        VersionedVolumeClient, "from_connect", return_value=client
    ) as from_connect:
        vol = VersionedVolume("models")

    from_connect.assert_called_once()
    assert vol.info.name == "models"
    client._inner.create_volume.assert_called_once_with("models")


def test_create_if_missing_false_propagates_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.side_effect = _not_found()
    vol = VersionedVolume("models", client=client, create_if_missing=False)

    with pytest.raises(VolumeNotFoundError):
        vol.reload()

    client._inner.create_volume.assert_not_called()


def test_put_file_delegates_to_native_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(),
    }
    client._inner.upload.return_value = [_status_dict()]
    vol = VersionedVolume("models", client=client)

    vol.put_file("models/latest.bin", "hello")

    client._inner.upload.assert_called_once_with(
        "models",
        [("models/latest.bin", b"hello")],
        {
            "chunk_size": _VERSIONED_CHUNK_SIZE_BYTES,
            "inline_threshold": _VERSIONED_INLINE_THRESHOLD_BYTES,
            "max_pack_bytes": _VERSIONED_MAX_PACK_BYTES,
            "file_concurrency": _VERSIONED_FILE_CONCURRENCY,
            "chunk_concurrency": _VERSIONED_CHUNK_CONCURRENCY,
            "batch_size": 1,
            "max_chunk_retries": _VERSIONED_CHUNK_MAX_RETRIES,
            "max_rate_limit_retries": _VERSIONED_RATE_LIMIT_MAX_RETRIES,
            "max_commit_retries": _VERSIONED_COMMIT_MAX_REBASE_RETRIES,
        },
    )
    assert vol.version == VersionInfo(
        id=11, sequence_number=21, ref="main", commit_id="commit-11"
    )


def test_batch_upload_microbatches_by_file_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(),
    }
    client._inner.upload.return_value = [_status_dict()]
    vol = VersionedVolume("models", client=client)

    with vol.batch_upload(max_files_per_commit=2) as batch:
        for index in range(5):
            batch.put(f"data/{index}.txt", f"value-{index}")

    calls = client._inner.upload.call_args_list
    assert [call.args[1] for call in calls] == [
        [("data/0.txt", b"value-0"), ("data/1.txt", b"value-1")],
        [("data/2.txt", b"value-2"), ("data/3.txt", b"value-3")],
        [("data/4.txt", b"value-4")],
    ]
    assert [call.args[2]["batch_size"] for call in calls] == [2, 2, 1]


def test_batch_upload_creates_missing_volume_before_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.side_effect = _not_found()
    client._inner.create_volume.return_value = {"volume": _volume_dict()}
    client._inner.upload.return_value = [_status_dict()]
    vol = VersionedVolume("models", client=client)

    with vol.batch_upload() as batch:
        batch.put("data/train.csv", "id,label\n")

    client._inner.create_volume.assert_called_once_with("models")
    client._inner.upload.assert_called_once()


def test_batch_upload_rejects_invalid_microbatch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    vol = VersionedVolume("models", client=client)

    with pytest.raises(ValueError, match="max_files_per_commit"):
        with vol.batch_upload(max_files_per_commit=0):
            pass


def test_put_file_from_path_passes_path_to_native(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(),
    }
    client._inner.upload.return_value = [_status_dict()]
    src = tmp_path / "model.bin"
    src.write_bytes(b"weights")

    VersionedVolume("models", client=client).put_file_from_path("model.bin", src)

    assert client._inner.upload.call_args.args[1] == [("model.bin", src)]


def test_read_file_delegates_to_native_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(),
    }
    client._inner.download_bytes.return_value = b"old"
    vol = VersionedVolume("models", client=client).at_version(7)

    assert vol.read_file("model.bin") == b"old"
    client._inner.download_bytes.assert_called_once_with(
        "models",
        "model.bin",
        7,
        None,
    )


def test_at_version_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    vol = VersionedVolume("models", client=client).at_version(7)

    with pytest.raises(VolumeError, match="read-only"):
        vol.put_file("model.bin", b"new")

    client._inner.upload.assert_not_called()


def test_closed_volume_operations_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    vol = VersionedVolume("models", client=client)
    vol.close()

    with pytest.raises(VolumeClosedError):
        _ = vol.info
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

    client._inner.get_volume.assert_not_called()
    client._inner.upload.assert_not_called()


def test_listdir_recurses_through_native_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(),
    }
    client._inner.list_files.side_effect = [
        {
            "files": [
                {"path": "dir", "size": 0, "kind": 2},
                {"path": "root.txt", "size": 4, "kind": 1},
            ],
            "next_cursor": "",
            "version": _version_dict(),
        },
        {
            "files": [{"path": "dir/file.txt", "size": 2, "kind": 1}],
            "next_cursor": "",
        },
    ]

    files = VersionedVolume("models", client=client).listdir()

    assert [f.path for f in files] == ["dir", "root.txt", "dir/file.txt"]


def test_versions_traverse_native_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(),
    }
    client._inner.list_volume_versions.side_effect = [
        {"versions": [_version_dict(1, 1)], "next_cursor": "next"},
        {"versions": [_version_dict(2, 2)], "next_cursor": ""},
    ]

    versions = VersionedVolume("models", client=client).versions(limit=1)

    assert [v.id for v in versions] == [1, 2]


def test_delete_delegates_to_native_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(),
    }

    VersionedVolume("models", client=client).delete()

    client._inner.delete_volume.assert_called_once_with("models")


def test_chunked_read_downloads_chunks_and_joins_by_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(vv, "_read_file_from_signed_uri", read)
    assert _read_versioned_file_response(resp) == b"hello world"


def test_dict_commit_status_conversion() -> None:
    assert vv._dict_to_commit_status(_status_dict()) == CommitStatus(
        commit_id="commit-1",
        result="committed",
        version=VersionInfo(
            id=11, sequence_number=21, ref="main", commit_id="commit-11"
        ),
    )
