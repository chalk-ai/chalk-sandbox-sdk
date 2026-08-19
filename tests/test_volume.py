from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from protobuf import Oneof

from chalkcompute._gen.chalk.volume.v2 import volume_pb as pb_v2
from chalkcompute._volume import VolumeClosedError, VolumeError, VolumeNotFoundError
from chalkcompute import _volume as vv
from chalkcompute._volume import (
    CommitStatus,
    ForkInfo,
    VersionInfo,
    Volume,
    VolumeClient,
    _CHUNK_MAX_RETRIES,
    _CHUNK_CONCURRENCY,
    _CHUNK_SIZE_BYTES,
    _COMMIT_MAX_REBASE_RETRIES,
    _FILE_CONCURRENCY,
    _INLINE_THRESHOLD_BYTES,
    _MAX_PACK_BYTES,
    _RATE_LIMIT_MAX_RETRIES,
    _read_file_response,
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


def _version_dict(version_id: int = 10) -> dict[str, Any]:
    return {
        "version_id": version_id,
        "ref": "main",
        "commit_id": f"commit-{version_id}",
    }


def _status_dict(version_id: int = 11) -> dict[str, Any]:
    return {
        "commit_id": "commit-1",
        "result": "committed",
        "version": _version_dict(version_id),
    }


def _make_client(monkeypatch: pytest.MonkeyPatch) -> VolumeClient:
    monkeypatch.setattr(vv, "_NativeVolumeRpcError", RuntimeError)
    client = object.__new__(VolumeClient)
    client._closed = False
    client._inner = MagicMock()
    client.close = MagicMock()
    return client


def _not_found() -> RuntimeError:
    return RuntimeError("not_found", "missing")


def test_volume_error_preserves_native_code_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    message = "volume v2 upstream returned 404 for CreateRef"
    client._inner.create_ref.side_effect = RuntimeError("unimplemented", message)

    with pytest.raises(VolumeError, match=message) as raised:
        client._create_ref("models", "sandbox-a", 7)

    assert raised.value.code == "unimplemented"


def test_volume_not_found_preserves_native_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.side_effect = RuntimeError(
        "not_found", "server volume lookup failed"
    )

    with pytest.raises(
        VolumeNotFoundError, match="server volume lookup failed"
    ) as raised:
        client.lookup("models")

    assert raised.value.code == "not_found"


def test_client_from_connect_uses_connect_http_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = MagicMock()
    monkeypatch.setattr(vv, "_NativeVolumeClient", native)
    connect = MagicMock()
    connect._api_server = "https://api.chalk.ai/"
    connect._access_token = "token"
    connect._env_id = "env-1"

    client = VolumeClient.from_connect(connect)

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


def test_client_from_connect_rebuilds_after_identity_token_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = MagicMock()
    monkeypatch.setattr(vv, "_NativeVolumeClient", native)

    class RotatingConnect:
        _api_server = "https://api.chalk.ai"
        _access_token = "token-one"
        _env_id = "env-one"

        def _ensure_auth(self) -> None:
            pass

        def _uses_web_identity_token(self) -> bool:
            return True

    connect = RotatingConnect()
    client = VolumeClient.from_connect(connect)  # type: ignore[arg-type]
    connect._access_token = "token-two"
    connect._env_id = "env-two"

    _ = client._inner

    assert native.call_count == 2
    native.assert_called_with(
        "https://api.chalk.ai",
        "token-two",
        "env-two",
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

    vol = Volume("models", client=client)

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
        VolumeClient, "from_connect", return_value=client
    ) as from_connect:
        vol = Volume("models")

    from_connect.assert_called_once()
    assert vol.info.name == "models"
    client._inner.create_volume.assert_called_once_with("models", None)


def test_create_if_missing_false_propagates_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.side_effect = _not_found()
    vol = Volume("models", client=client, create_if_missing=False)

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
    vol = Volume("models", client=client)

    vol.put_file("models/latest.bin", "hello")

    client._inner.upload.assert_called_once_with(
        "models",
        [("models/latest.bin", b"hello")],
        {
            "chunk_size": _CHUNK_SIZE_BYTES,
            "inline_threshold": _INLINE_THRESHOLD_BYTES,
            "max_pack_bytes": _MAX_PACK_BYTES,
            "file_concurrency": _FILE_CONCURRENCY,
            "chunk_concurrency": _CHUNK_CONCURRENCY,
            "batch_size": 1,
            "max_chunk_retries": _CHUNK_MAX_RETRIES,
            "max_rate_limit_retries": _RATE_LIMIT_MAX_RETRIES,
            "max_commit_retries": _COMMIT_MAX_REBASE_RETRIES,
        },
    )
    assert vol.version == VersionInfo(id=11, ref="main", commit_id="commit-11")


def test_batch_upload_microbatches_by_file_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(),
    }
    client._inner.upload.return_value = [_status_dict()]
    vol = Volume("models", client=client)

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
    vol = Volume("models", client=client)

    with vol.batch_upload() as batch:
        batch.put("data/train.csv", "id,label\n")

    client._inner.create_volume.assert_called_once_with("models", None)
    client._inner.upload.assert_called_once()


def test_batch_upload_rejects_invalid_microbatch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    vol = Volume("models", client=client)

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

    Volume("models", client=client).put_file_from_path("model.bin", src)

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
    vol = Volume("models", client=client).at_version(7)

    assert vol.read_file("model.bin") == b"old"
    client._inner.download_bytes.assert_called_once_with(
        "models",
        "model.bin",
        7,
        None,
    )


def test_at_version_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    vol = Volume("models", client=client).at_version(7)

    with pytest.raises(VolumeError, match="read-only"):
        vol.put_file("model.bin", b"new")

    client._inner.upload.assert_not_called()


def test_fork_creates_ref_from_pinned_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.create_ref.return_value = {
        "ref": {
            "name": "sandbox-a",
            "version_id": 7,
            "commit_id": "base-commit",
            "parent_version_id": 7,
        }
    }

    fork = Volume("models", client=client).at_version(7).fork("sandbox-a")

    client._inner.create_ref.assert_called_once_with("models", "sandbox-a", 7)
    assert fork.ref == "sandbox-a"
    assert fork.version == VersionInfo(
        id=7,
        ref="sandbox-a",
        commit_id="base-commit",
    )


def test_forks_can_be_chained(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(7),
    }
    client._inner.create_ref.side_effect = [
        {"ref": {"name": "test-fork", "version_id": 7}},
        {
            "ref": {
                "name": "chained-fork",
                "version_id": 7,
            }
        },
    ]

    chained = Volume("test", client=client).fork("test-fork").fork("chained-fork")

    assert [call.args for call in client._inner.create_ref.call_args_list] == [
        ("test", "test-fork", 7),
        ("test", "chained-fork", 7),
    ]
    assert chained.ref == "chained-fork"


def test_fork_writes_and_lists_versions_on_its_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(7),
    }
    client._inner.create_ref.return_value = {
        "ref": {"name": "sandbox-a", "version_id": 7}
    }
    client._inner.upload.return_value = [_status_dict(8)]
    client._inner.list_volume_versions.return_value = {
        "versions": [_version_dict(8)],
        "next_cursor": "",
    }
    fork = Volume("models", client=client).fork("sandbox-a")

    fork.put_file("result.txt", b"done")
    versions = fork.versions()

    assert client._inner.upload.call_args.args[-1] == "sandbox-a"
    client._inner.list_volume_versions.assert_called_once_with(
        "models", 100, "", "sandbox-a"
    )
    assert [version.id for version in versions] == [8]


def test_forks_and_delete_fork_delegate_to_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(),
    }
    client._inner.list_refs.return_value = [
        {
            "name": "main",
            "version_id": 10,
        },
        {
            "name": "sandbox-a",
            "version_id": 7,
            "parent_version_id": 4,
        },
    ]
    volume = Volume("models", client=client, ref="sandbox-a")

    assert volume.forks() == [
        ForkInfo(
            name="sandbox-a",
            version_id=7,
            parent_version_id=4,
        )
    ]
    volume.delete_fork()

    client._inner.list_refs.assert_called_once_with("models")
    client._inner.delete_ref.assert_called_once_with("models", "sandbox-a")


def test_fork_mount_uses_default_path_and_pins_tip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    fork = Volume("models", client=client, ref="sandbox-a")
    fork._version = VersionInfo(id=7, ref="sandbox-a")

    mount = fork.mount()

    assert mount.name == "models"
    assert mount.mount_path == "/volumes/models"
    assert mount.version_id == 7
    assert mount.to_spec_dict()["versionId"] == 7


def test_closed_volume_operations_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    vol = Volume("models", client=client)
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
        vol.fork("branch")
    with pytest.raises(VolumeClosedError):
        vol.forks()
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

    files = Volume("models", client=client).listdir()

    assert [f.path for f in files] == ["dir", "root.txt", "dir/file.txt"]


def test_versions_traverse_native_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(),
    }
    client._inner.list_volume_versions.side_effect = [
        {"versions": [_version_dict(1)], "next_cursor": "next"},
        {"versions": [_version_dict(2)], "next_cursor": ""},
    ]

    versions = Volume("models", client=client).versions(limit=1)

    assert [v.id for v in versions] == [1, 2]


def test_delete_delegates_to_native_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(),
    }

    Volume("models", client=client).delete()

    client._inner.delete_volume.assert_called_once_with("models")


def test_chunked_read_downloads_chunks_and_joins_by_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = pb_v2.GetFileResponse(
        content=Oneof(
            "chunked",
            pb_v2.ChunkedFileContent(
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
            ),
        )
    )

    def read(uri: str) -> bytes:
        return {
            "https://storage/chunk-1": b"hello ",
            "https://storage/chunk-2": b"world",
        }[uri]

    monkeypatch.setattr(vv, "_read_file_from_signed_uri", read)
    assert _read_file_response(resp) == b"hello world"


def test_signed_download_uses_shared_pyqwest_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_client = MagicMock()
    http_client.get.return_value.status = 200
    http_client.get.return_value.content = b"downloaded"
    monkeypatch.setattr(vv, "SYNC_HTTP_CLIENT", http_client)

    assert vv._read_file_from_signed_uri("https://storage/chunk") == b"downloaded"
    http_client.get.assert_called_once_with("https://storage/chunk", timeout=300)


def test_signed_download_rejects_unsuccessful_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_client = MagicMock()
    http_client.get.return_value.status = 403
    monkeypatch.setattr(vv, "SYNC_HTTP_CLIENT", http_client)

    with pytest.raises(VolumeError, match="failed with status 403"):
        vv._read_file_from_signed_uri("https://storage/chunk")


def test_signed_download_translates_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_client = MagicMock()
    http_client.get.side_effect = TimeoutError("slow")
    monkeypatch.setattr(vv, "SYNC_HTTP_CLIENT", http_client)

    with pytest.raises(VolumeError, match="Timed out downloading signed file URL"):
        vv._read_file_from_signed_uri("https://storage/chunk")


def test_dict_commit_status_conversion() -> None:
    assert vv._dict_to_commit_status(_status_dict()) == CommitStatus(
        commit_id="commit-1",
        result="committed",
        version=VersionInfo(id=11, ref="main", commit_id="commit-11"),
    )


def _lazy_image(tmp_path, dest: str = "/app/f.txt"):
    from chalkcompute._image import Image, LazyLocalFile

    src = tmp_path / "f.txt"
    src.write_bytes(b"hi")
    image = Image.base("python:3.14")
    image._lazy_local_files = {LazyLocalFile(src=str(src), dest=dest)}
    return image, src


def test_stage_lazy_files_creates_source_code_volume_and_returns_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from pathlib import Path

    image, src = _lazy_image(tmp_path)
    client = _make_client(monkeypatch)
    client._inner.get_volume.side_effect = _not_found()
    client._inner.create_volume.return_value = {"volume": _volume_dict("src-vol")}
    intent = pb_v2.CommitIntent(
        volume=pb_v2.VolumeRef(name="src-vol"), author="chalk:env:agent:a"
    )
    client._inner.staged_intent.return_value = intent.to_binary()

    volumes, intents = client.stage_lazy_files(image, volume_name="fn-src-add")

    client._inner.create_volume.assert_called_once_with("fn-src-add-app", "source_code")

    (staged_name, source), _ = client._inner.stage.call_args
    assert staged_name == "fn-src-add-app"
    assert source == [("f.txt", Path(str(src)))]

    assert intents == [intent]
    assert len(volumes) == 1
    assert volumes[0].name == "fn-src-add-app"
    assert volumes[0].mount_path == "/app"
    assert volumes[0].to_spec_dict()["type"] == "versioned_chalkfs"
    # Nothing commits client-side: the intent lands via volume_commits.
    client._inner.commit_staged.assert_not_called()
    client._inner.upload.assert_not_called()


def test_stage_lazy_files_reuses_volume_and_replaces_base_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    image, _src = _lazy_image(tmp_path)
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict("fn-src-add-app"),
        "version": _version_dict(),
    }
    intent = pb_v2.CommitIntent(volume=pb_v2.VolumeRef(name="fn-src-add-app"))
    client._inner.staged_intent.return_value = intent.to_binary()

    volumes, intents = client.stage_lazy_files(image, volume_name="fn-src-add")

    client._inner.create_volume.assert_not_called()
    assert volumes[0].name == "fn-src-add-app"
    # A recursive root remove tells the server to build the version from
    # an empty tree plus the upserts.
    assert intents[0].deltas is not None
    assert intents[0].deltas.field == "path_deltas"
    assert intents[0].deltas.value.removes == [
        pb_v2.PathRemoveDelta(path="/", recursive=True)
    ]


def test_native_client_symbol_is_bound() -> None:
    from chalkcompute import _volume

    if _volume._native is None:
        pytest.skip("native extension not built")
    assert _volume._NativeVolumeClient is not None
    assert _volume._NativeVolumeRpcError is not None
