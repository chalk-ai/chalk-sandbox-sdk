from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from protobuf import Oneof

from chalkcompute._evaluation import DatasetRevisionRef
from chalkcompute._gen.chalk.server.v1 import datasets_pb, github_app_pb
from chalkcompute._gen.chalk.volume.v2 import volume_pb as pb_v2
from chalkcompute._volume import (
    VolumeClosedError,
    VolumeError,
    VolumeFileNotFoundError,
    VolumeNotFoundError,
)
from chalkcompute import _volume as vv
from chalkcompute._volume import (
    CommitStatus,
    Diff,
    FileChange,
    ForkInfo,
    VersionInfo,
    Volume,
    VolumeClient,
    VolumeMount,
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
from tests._fakes import FakeConnectClient


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


def test_read_file_maps_missing_file_to_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(),
    }
    client._inner.download_bytes.side_effect = RuntimeError(
        "not_found", 'file "model.bin" not found'
    )

    with pytest.raises(VolumeFileNotFoundError, match='file "model.bin" not found'):
        Volume("models", client=client).read_file("model.bin")


def test_at_version_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    vol = Volume("models", client=client).at_version(7)

    with pytest.raises(VolumeError, match="read-only"):
        vol.put_file("model.bin", b"new")

    client._inner.upload.assert_not_called()


def test_at_ref_opens_mutable_ref_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)

    ref = Volume("models", client=client).at_ref("sandbox-a")

    assert ref.name == "models"
    assert ref.ref == "sandbox-a"
    assert ref.version_id is None


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


def test_fork_mount_uses_default_path_and_preserves_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    fork = Volume("models", client=client, ref="sandbox-a")
    fork._version = VersionInfo(id=7, ref="sandbox-a")

    mount = fork.mount()

    assert mount.name == "models"
    assert mount.mount_path == "/volumes/models"
    assert mount.version_id is None
    assert mount.ref_name == "sandbox-a"
    assert mount.to_spec_dict()["refName"] == "sandbox-a"


def test_volume_mount_rejects_ambiguous_selector() -> None:
    with pytest.raises(ValueError, match="cannot both be set"):
        VolumeMount(
            name="models",
            mount_path="/models",
            version_id=7,
            ref_name="sandbox-a",
        )


def test_volume_mount_rejects_empty_ref() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        VolumeMount(name="models", mount_path="/models", ref_name="")


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


def test_resolve_github_installation_rejects_missing_owner_slash() -> None:
    connect = FakeConnectClient()

    with pytest.raises(VolumeError, match="owner/name"):
        vv._resolve_github_installation("no-slash-here", connect=connect, timeout=30.0)  # type: ignore[arg-type]


def test_resolve_github_installation_raises_when_no_installations() -> None:
    def handler(method: str, request: Any) -> Any:
        if method == "list_git_hub_app_installations":
            return github_app_pb.ListGitHubAppInstallationsResponse(installations=[])
        raise AssertionError(method)

    connect = FakeConnectClient(handler=handler)

    with pytest.raises(VolumeError, match="no GitHub App installations"):
        vv._resolve_github_installation(
            "chalk-ai/customer-deployment", connect=connect, timeout=30.0  # type: ignore[arg-type]
        )


def test_resolve_github_installation_paginates_and_tries_next_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vv, "_GITHUB_REPOS_PER_PAGE", 2)
    repo_calls: list[tuple[str, int]] = []

    def handler(method: str, request: Any) -> Any:
        if method == "list_git_hub_app_installations":
            return github_app_pb.ListGitHubAppInstallationsResponse(
                installations=[
                    github_app_pb.GitHubAppInstallation(id="inst-1"),
                    github_app_pb.GitHubAppInstallation(id="inst-2"),
                ]
            )
        if method == "list_git_hub_repositories":
            repo_calls.append((request.installation_id, request.page))
            if request.installation_id == "inst-1" and request.page == 1:
                return github_app_pb.ListGitHubRepositoriesResponse(
                    repositories=[
                        github_app_pb.GitHubRepository(full_name="chalk-ai/other-a"),
                        github_app_pb.GitHubRepository(full_name="chalk-ai/other-b"),
                    ]
                )
            if request.installation_id == "inst-1" and request.page == 2:
                return github_app_pb.ListGitHubRepositoriesResponse(repositories=[])
            if request.installation_id == "inst-2" and request.page == 1:
                return github_app_pb.ListGitHubRepositoriesResponse(
                    repositories=[
                        github_app_pb.GitHubRepository(
                            full_name="Chalk-AI/Customer-Deployment"
                        ),
                    ]
                )
            raise AssertionError((request.installation_id, request.page))
        raise AssertionError(method)

    connect = FakeConnectClient(handler=handler)

    result = vv._resolve_github_installation(
        "chalk-ai/customer-deployment", connect=connect, timeout=30.0  # type: ignore[arg-type]
    )

    assert result == ("inst-2", "chalk-ai", "customer-deployment")
    assert repo_calls == [("inst-1", 1), ("inst-1", 2), ("inst-2", 1)]


def test_resolve_github_installation_raises_when_repo_not_found() -> None:
    def handler(method: str, request: Any) -> Any:
        if method == "list_git_hub_app_installations":
            return github_app_pb.ListGitHubAppInstallationsResponse(
                installations=[github_app_pb.GitHubAppInstallation(id="inst-1")]
            )
        if method == "list_git_hub_repositories":
            return github_app_pb.ListGitHubRepositoriesResponse(repositories=[])
        raise AssertionError(method)

    connect = FakeConnectClient(handler=handler)

    with pytest.raises(VolumeError, match="was not found in any linked"):
        vv._resolve_github_installation(
            "chalk-ai/customer-deployment", connect=connect, timeout=30.0  # type: ignore[arg-type]
        )


def _github_resolution_handler(method: str, request: Any) -> Any:
    if method == "list_git_hub_app_installations":
        return github_app_pb.ListGitHubAppInstallationsResponse(
            installations=[github_app_pb.GitHubAppInstallation(id="inst-1")]
        )
    if method == "list_git_hub_repositories":
        return github_app_pb.ListGitHubRepositoriesResponse(
            repositories=[
                github_app_pb.GitHubRepository(full_name="chalk-ai/customer-deployment")
            ]
        )
    raise AssertionError(method)


def test_from_github_creates_volume_via_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Any] = []

    def handler(method: str, request: Any) -> Any:
        if method == "create_volume_from_git_hub_repo":
            requests.append(request)
            return github_app_pb.CreateVolumeFromGitHubRepoResponse(
                volume_name=request.volume_name, volume_id="vol-1", files=3, bytes=42
            )
        return _github_resolution_handler(method, request)

    connect = FakeConnectClient(handler=handler)
    client = _make_client(monkeypatch)

    with patch.object(VolumeClient, "from_connect", return_value=client):
        volume = Volume.from_github(
            "chalk-ai/customer-deployment", ref="main", connect_client=connect  # type: ignore[arg-type]
        )

    assert volume.name.startswith("github-chalk-ai-customer-deployment-")
    assert len(requests) == 1
    request = requests[0]
    assert request.installation_id == "inst-1"
    assert request.owner == "chalk-ai"
    assert request.repo == "customer-deployment"
    assert request.ref == "main"
    assert request.volume_name == volume.name


def test_from_github_wraps_rpc_errors() -> None:
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError as ConnectRpcError

    def handler(method: str, request: Any) -> Any:
        if method == "create_volume_from_git_hub_repo":
            raise ConnectRpcError(Code.INTERNAL, "boom")
        return _github_resolution_handler(method, request)

    connect = FakeConnectClient(handler=handler)

    with pytest.raises(VolumeError, match="failed to create volume"):
        Volume.from_github(
            "chalk-ai/customer-deployment", connect_client=connect  # type: ignore[arg-type]
        )
def test_version_diff_rejects_non_version_info(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(),
    }
    version = Volume("models", client=client).version

    with pytest.raises(TypeError, match="expected a VersionInfo"):
        version.diff("not-a-version")  # type: ignore[arg-type]


def test_version_diff_requires_both_sides_from_a_volume_handle() -> None:
    detached = VersionInfo(id=1, ref="main")
    attached = VersionInfo(id=2, ref="main")
    attached._source = object.__new__(Volume)
    attached._source._name = "models"  # type: ignore[attr-defined]

    with pytest.raises(VolumeError, match="requires both to come from a Volume handle"):
        attached.diff(detached)
    with pytest.raises(VolumeError, match="requires both to come from a Volume handle"):
        detached.diff(attached)


def test_version_diff_rejects_cross_volume_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict("models"),
        "version": _version_dict(1),
    }
    new_version = Volume("models", client=client).version

    other_client = _make_client(monkeypatch)
    other_client._inner.get_volume.return_value = {
        "volume": _volume_dict("other"),
        "version": _version_dict(2),
    }
    old_version = Volume("other", client=other_client).version

    with pytest.raises(VolumeError, match="cannot diff versions from different volumes"):
        new_version.diff(old_version)


def test_version_diff_detects_added_modified_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.get_volume.return_value = {
        "volume": _volume_dict(),
        "version": _version_dict(20),
    }
    client._inner.list_volume_versions.return_value = {
        "versions": [_version_dict(10), _version_dict(20)],
        "next_cursor": "",
    }
    vol = Volume("models", client=client)
    old_version, new_version = vol.versions()

    def list_files(volume, path, version_id, ref_name, limit, cursor):
        if version_id == 10:
            files = [
                {"path": "same.txt", "size": 1, "kind": 1, "hash": "h-same"},
                {"path": "removed.txt", "size": 1, "kind": 1, "hash": "h-removed"},
                {"path": "changed.txt", "size": 1, "kind": 1, "hash": "h-changed-old"},
            ]
        else:
            files = [
                {"path": "same.txt", "size": 1, "kind": 1, "hash": "h-same"},
                {"path": "added.txt", "size": 1, "kind": 1, "hash": "h-added"},
                {"path": "changed.txt", "size": 1, "kind": 1, "hash": "h-changed-new"},
            ]
        return {"files": files, "next_cursor": ""}

    client._inner.list_files.side_effect = list_files

    downloads = {
        (10, "removed.txt"): b"gone",
        (10, "changed.txt"): b"before",
        (20, "added.txt"): b"new file",
        (20, "changed.txt"): b"after",
    }
    client._inner.download_bytes.side_effect = (
        lambda volume, path, version_id, _: downloads[(version_id, path)]
    )

    diff = new_version.diff(old_version)

    assert diff.volume_name == "models"
    assert diff.old_version_id == 10
    assert diff.new_version_id == 20
    changes = {change.path: change for change in diff.changes}
    assert set(changes) == {"added.txt", "removed.txt", "changed.txt"}
    assert changes["added.txt"] == FileChange(
        path="added.txt", kind="added", new_content=b"new file"
    )
    assert changes["removed.txt"] == FileChange(
        path="removed.txt", kind="deleted", old_content=b"gone"
    )
    assert changes["changed.txt"] == FileChange(
        path="changed.txt", kind="modified", old_content=b"before", new_content=b"after"
    )


def test_diff_bool_len_and_iter_reflect_changes() -> None:
    empty = Diff(volume_name="models", old_version_id=1, new_version_id=2, changes=())
    assert not empty
    assert len(empty) == 0
    assert list(empty) == []

    change = FileChange(path="a.txt", kind="added", new_content=b"hi")
    nonempty = Diff(
        volume_name="models", old_version_id=1, new_version_id=2, changes=(change,)
    )
    assert nonempty
    assert len(nonempty) == 1
    assert list(nonempty) == [change]


def test_diff_unified_diff_renders_text_changes() -> None:
    diff = Diff(
        volume_name="models",
        old_version_id=1,
        new_version_id=2,
        changes=(
            FileChange(
                path="a.txt",
                kind="modified",
                old_content=b"line1\nline2\n",
                new_content=b"line1\nline2 changed\n",
            ),
        ),
    )

    rendered = diff.unified_diff()

    assert "--- a/a.txt" in rendered
    assert "+++ b/a.txt" in rendered
    assert "-line2" in rendered
    assert "+line2 changed" in rendered
    assert str(diff) == rendered


def test_diff_unified_diff_reports_binary_files_without_content() -> None:
    diff = Diff(
        volume_name="models",
        old_version_id=1,
        new_version_id=2,
        changes=(
            FileChange(
                path="weights.bin",
                kind="modified",
                old_content=b"\xff\xfe\x00",
                new_content=b"\x00\xfe\xff",
            ),
        ),
    )

    assert diff.unified_diff() == "Binary files a/weights.bin and b/weights.bin differ\n"


def _list_datasets_handler(*, dataset_name: str, offline_query_id: str | None):
    requests: list[Any] = []

    def handler(method: str, request: Any) -> Any:
        if method == "list_datasets":
            requests.append(request)
            revision = (
                None
                if offline_query_id is None
                else datasets_pb.DatasetRevisionMeta(offline_query_id=offline_query_id)
            )
            return datasets_pb.ListDatasetsResponse(
                datasets=[
                    datasets_pb.DatasetMeta(
                        dataset_name=dataset_name,
                        most_recent_revision=revision,
                    )
                ]
            )
        raise AssertionError(f"unexpected method: {method}")

    return handler, requests


def test_from_dataset_resolves_bare_string_and_uploads_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler, requests = _list_datasets_handler(
        dataset_name="fraud_labels", offline_query_id="rev-1"
    )

    def full_handler(method: str, request: Any) -> Any:
        if method == "get_dataset_revision_download_links":
            assert request.revision_id == "rev-1"
            return datasets_pb.GetDatasetRevisionDownloadLinksResponse(
                output_urls=["https://storage/part-0", "https://storage/part-1"]
            )
        return handler(method, request)

    connect = FakeConnectClient(handler=full_handler)
    contents = {
        "https://storage/part-0": b"parquet-bytes-0",
        "https://storage/part-1": b"parquet-bytes-1",
    }
    monkeypatch.setattr(vv, "_read_file_from_signed_uri", lambda uri: contents[uri])

    client = _make_client(monkeypatch)
    client._inner.get_volume.side_effect = _not_found()
    client._inner.create_volume.return_value = {"volume": _volume_dict("dataset-vol")}
    client._inner.upload.return_value = [_status_dict()]

    with patch.object(VolumeClient, "from_connect", return_value=client):
        volume = Volume.from_dataset("fraud_labels", connect_client=connect)  # type: ignore[arg-type]

    assert requests[0].search == "fraud_labels"
    assert volume.name.startswith("dataset-fraud_labels-")
    assert client._inner.upload.call_args.args[1] == [
        ("part-00000.parquet", b"parquet-bytes-0"),
        ("part-00001.parquet", b"parquet-bytes-1"),
    ]


def test_from_dataset_accepts_explicit_revision_ref_and_skips_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(method: str, request: Any) -> Any:
        if method == "list_datasets":
            raise AssertionError("should not resolve by name when a revision is given")
        if method == "get_dataset_revision_download_links":
            assert request.revision_id == "rev-42"
            return datasets_pb.GetDatasetRevisionDownloadLinksResponse(
                output_urls=["https://storage/part-0"]
            )
        raise AssertionError(f"unexpected method: {method}")

    connect = FakeConnectClient(handler=handler)
    monkeypatch.setattr(vv, "_read_file_from_signed_uri", lambda uri: b"parquet-bytes")

    client = _make_client(monkeypatch)
    client._inner.get_volume.side_effect = _not_found()
    client._inner.create_volume.return_value = {"volume": _volume_dict("dataset-vol")}
    client._inner.upload.return_value = [_status_dict()]

    ref = DatasetRevisionRef(dataset_id="ds-1", revision_id="rev-42")
    with patch.object(VolumeClient, "from_connect", return_value=client):
        Volume.from_dataset(ref, connect_client=connect, volume_name="pinned")  # type: ignore[arg-type]

    client._inner.create_volume.assert_called_once_with("pinned", None)


def test_from_dataset_raises_when_dataset_name_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler, _requests = _list_datasets_handler(
        dataset_name="other_dataset", offline_query_id="rev-1"
    )
    connect = FakeConnectClient(handler=handler)

    with pytest.raises(VolumeError, match="no dataset named 'fraud_labels'"):
        Volume.from_dataset("fraud_labels", connect_client=connect)  # type: ignore[arg-type]


def test_from_dataset_raises_when_dataset_has_no_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler, _requests = _list_datasets_handler(
        dataset_name="fraud_labels", offline_query_id=None
    )
    connect = FakeConnectClient(handler=handler)

    with pytest.raises(VolumeError, match="has no revisions"):
        Volume.from_dataset("fraud_labels", connect_client=connect)  # type: ignore[arg-type]


def test_from_dataset_raises_when_revision_has_no_output_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler, _requests = _list_datasets_handler(
        dataset_name="fraud_labels", offline_query_id="rev-1"
    )

    def full_handler(method: str, request: Any) -> Any:
        if method == "get_dataset_revision_download_links":
            return datasets_pb.GetDatasetRevisionDownloadLinksResponse(output_urls=[])
        return handler(method, request)

    connect = FakeConnectClient(handler=full_handler)

    with pytest.raises(VolumeError, match="has no output files"):
        Volume.from_dataset("fraud_labels", connect_client=connect)  # type: ignore[arg-type]
