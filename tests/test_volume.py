from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf import Oneof

from chalkcompute._gen.chalk.volume.v2 import volume_pb as pb_v2
from chalkcompute._gen.chalk.manager.v1 import dataset_download_pb
from chalkcompute._volume import (
    VolumeClosedError,
    VolumeError,
    VolumeFileNotFoundError,
    VolumeNotFoundError,
)
from chalkcompute import _volume as vv
from chalkcompute._volume import (
    CommitStatus,
    ForkInfo,
    VersionInfo,
    Volume,
    VolumeClient,
    VolumeMount,
    _BATCH_MAX_FILES,
    _CHUNK_MAX_RETRIES,
    _CHUNK_CONCURRENCY,
    _CHUNK_SIZE_BYTES,
    _COMMIT_MAX_REBASE_RETRIES,
    _FILE_CONCURRENCY,
    _MAX_PACK_BYTES,
    _RATE_LIMIT_MAX_RETRIES,
    _dataset_output_path,
    _read_file_response,
)


@pytest.fixture(autouse=True)
def _disable_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHALK_REMOTE_FUNCTION_TRACE_POLICY", "always_off")


def _volume_proto(name: str = "models") -> pb_v2.VolumeInfo:
    return pb_v2.VolumeInfo(
        volume_id="vol-123",
        name=name,
        access_mode=pb_v2.VolumeAccessMode.READ_WRITE,
        ref="main",
    )


def _version_proto(version_id: int = 10) -> pb_v2.VersionInfo:
    return pb_v2.VersionInfo(
        version_id=version_id,
        sequence_number=version_id,
        ref="main",
        commit_id=f"commit-{version_id}",
    )


def _status_dict(
    version_id: int = 11,
    *,
    ref: str = "main",
) -> dict[str, Any]:
    return {
        "commit_id": "commit-1",
        "result": "committed",
        "version": {
            "version_id": version_id,
            "ref": ref,
            "commit_id": f"commit-{version_id}",
        },
    }


def _get_volume_response(
    name: str = "models", version_id: int = 10
) -> pb_v2.GetVolumeResponse:
    return pb_v2.GetVolumeResponse(
        volume=_volume_proto(name),
        version=_version_proto(version_id),
    )


def _prepare_upload(
    client: VolumeClient,
    *,
    ref: str | None = None,
    version_id: int = 11,
) -> None:
    client._inner.upload.return_value = [_status_dict(version_id, ref=ref or "main")]


def _make_client(monkeypatch: pytest.MonkeyPatch) -> VolumeClient:
    monkeypatch.setattr(vv, "_NativeVolumeRpcError", RuntimeError)
    client = object.__new__(VolumeClient)
    client._closed = False
    client._owns_connect = False
    client._extra_headers = []
    client._chalk_server = "go-api"
    client._inner = MagicMock()
    client._connect = MagicMock()
    client._connect._access_token = "token"
    client._connect._env_id = "env-1"
    client._rpc = MagicMock()
    client._connect.stub.return_value = client._rpc
    client._connect.call.side_effect = lambda method, request, **_kwargs: method(
        request
    )
    client.close = MagicMock()
    return client


def _not_found() -> ConnectRpcError:
    return ConnectRpcError(Code.NOT_FOUND, "missing")


def test_volume_error_preserves_connect_code_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    message = "volume v2 upstream returned 404 for CreateRef"
    client._rpc.create_ref.side_effect = ConnectRpcError(Code.UNIMPLEMENTED, message)

    with pytest.raises(VolumeError, match=message) as raised:
        client._create_ref("models", "sandbox-a", 7)

    assert raised.value.code == "unimplemented"


def test_volume_not_found_preserves_connect_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.side_effect = ConnectRpcError(
        Code.NOT_FOUND, "server volume lookup failed"
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
    # commit_author is None so the native client derives the agent URN
    # from the credentials.
    native.assert_called_once_with(
        "https://api.chalk.ai",
        "token",
        "env-1",
        None,
        [],
        "go-api",
        client._refresh_token,
    )


def test_client_from_connect_refreshes_rotated_identity_token(
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

    connect = RotatingConnect()
    client = VolumeClient.from_connect(connect)  # type: ignore[arg-type]
    connect._access_token = "token-two"
    connect._env_id = "env-two"

    token = client._refresh_token()

    assert token == "token-two"
    assert native.call_count == 1


def test_accepts_explicit_client_and_gets_existing_volume_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.return_value = _get_volume_response()

    vol = Volume("models", client=client)

    assert vol.name == "models"
    assert vol._client is client
    client._rpc.get_volume.assert_not_called()
    assert vol.info.name == "models"
    assert vol.version.id == 10
    request = client._rpc.get_volume.call_args.args[0]
    assert request == pb_v2.GetVolumeRequest(volume=pb_v2.VolumeRef(name="models"))


def test_accepts_name_only_and_creates_missing_volume_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.side_effect = _not_found()
    client._rpc.create_volume.return_value = pb_v2.CreateVolumeResponse(
        volume=_volume_proto()
    )

    with patch.object(
        VolumeClient, "from_connect", return_value=client
    ) as from_connect:
        vol = Volume("models")

    from_connect.assert_called_once()
    assert vol.info.name == "models"
    client._rpc.create_volume.assert_called_once_with(
        pb_v2.CreateVolumeRequest(
            name="models", volume_type=pb_v2.VolumeKind.UNSPECIFIED
        )
    )


def test_create_if_missing_false_propagates_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.side_effect = _not_found()
    vol = Volume("models", client=client, create_if_missing=False)

    with pytest.raises(VolumeNotFoundError):
        vol.reload()

    client._rpc.create_volume.assert_not_called()


def test_put_file_delegates_to_native_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.return_value = _get_volume_response()
    _prepare_upload(client)
    vol = Volume("models", client=client)

    vol.put_file("models/latest.bin", "hello")

    client._inner.upload.assert_called_once_with(
        "models",
        [("models/latest.bin", b"hello")],
        {
            "chunk_size": _CHUNK_SIZE_BYTES,
            "max_pack_bytes": _MAX_PACK_BYTES,
            "file_concurrency": _FILE_CONCURRENCY,
            "chunk_concurrency": _CHUNK_CONCURRENCY,
            "batch_size": _BATCH_MAX_FILES,
            "max_chunk_retries": _CHUNK_MAX_RETRIES,
            "max_rate_limit_retries": _RATE_LIMIT_MAX_RETRIES,
            "max_commit_retries": _COMMIT_MAX_REBASE_RETRIES,
        },
        None,
    )
    client._rpc.commit_version.assert_not_called()
    assert vol.version == VersionInfo(id=11, ref="main", commit_id="commit-11")


def test_batch_upload_microbatches_by_file_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.return_value = _get_volume_response()
    _prepare_upload(client)
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
    assert [call.args[2]["batch_size"] for call in calls] == [
        _BATCH_MAX_FILES,
        _BATCH_MAX_FILES,
        _BATCH_MAX_FILES,
    ]


def test_batch_upload_creates_missing_volume_before_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.side_effect = [
        _not_found(),
        _get_volume_response(),
    ]
    client._rpc.create_volume.return_value = pb_v2.CreateVolumeResponse(
        volume=_volume_proto()
    )
    _prepare_upload(client)
    vol = Volume("models", client=client)

    with vol.batch_upload() as batch:
        batch.put("data/train.csv", "id,label\n")

    client._rpc.create_volume.assert_called_once()
    client._inner.upload.assert_called_once()


def test_batch_upload_rejects_invalid_microbatch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    vol = Volume("models", client=client)

    with pytest.raises(ValueError, match="max_files_per_commit"):
        with vol.batch_upload(max_files_per_commit=0):
            pass


@pytest.mark.parametrize(
    ("object_path", "dest_prefix", "expected"),
    [
        (
            "env_dev/job_1/query_2/outputs/partition_0/shard_1/chunk_2.parquet",
            "",
            "partition_0/shard_1/chunk_2.parquet",
        ),
        (
            "env_dev/job_1/query_2/outputs/partition_0/shard_1/chunk_2.parquet",
            "/training/run-1/",
            "training/run-1/partition_0/shard_1/chunk_2.parquet",
        ),
    ],
)
def test_dataset_output_path(object_path: str, dest_prefix: str, expected: str) -> None:
    assert _dataset_output_path(object_path, dest_prefix) == expected


@pytest.mark.parametrize(
    "object_path",
    [
        "env_dev/job_1/query_2/givens/chunk.parquet",
        "env_dev/job_1/query_2/outputs/../chunk.parquet",
    ],
)
def test_dataset_output_path_rejects_invalid_paths(object_path: str) -> None:
    with pytest.raises(ValueError, match="invalid dataset output path"):
        _dataset_output_path(object_path)


def test_load_dataset_retries_and_commits_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.return_value = _get_volume_response()
    entries = [
        dataset_download_pb.StreamDatasetDownloadURIsResponse(
            signed_url=f"https://objects/{index}",
            object_path=(
                "env_dev/job_1/query_2/outputs/"
                f"partition_0/shard_0/chunk_{index}.parquet"
            ),
            size_bytes=index + 1,
        )
        for index in range(3)
    ]
    client._rpc.stream_dataset_download_ur_is.side_effect = [entries, entries]
    first_staged = MagicMock()
    final_staged = MagicMock()
    client._inner.stage_remote.side_effect = [
        first_staged,
        RuntimeError("unavailable", "signed URL expired"),
        final_staged,
    ]
    client._inner.commit_staged.return_value = _status_dict()
    monkeypatch.setattr(vv, "_DATASET_INGEST_MAX_FILES", 2)
    progress: list[tuple[int, int]] = []
    vol = Volume("models", client=client)

    version = vol.load_dataset(
        type("Revision", (), {"revision_id": "revision-1"})(),
        dest_prefix="dataset",
        on_progress=lambda files, size: progress.append((files, size)),
    )

    assert version == VersionInfo(id=11, ref="main", commit_id="commit-11")
    assert client._rpc.stream_dataset_download_ur_is.call_count == 2
    stage_calls = client._inner.stage_remote.call_args_list
    assert len(stage_calls) == 3
    assert [item[0] for item in stage_calls[0].args[1]] == [
        "dataset/partition_0/shard_0/chunk_0.parquet",
        "dataset/partition_0/shard_0/chunk_1.parquet",
    ]
    assert [item[0] for item in stage_calls[1].args[1]] == [
        "dataset/partition_0/shard_0/chunk_2.parquet"
    ]
    assert stage_calls[2].args[1] == stage_calls[1].args[1]
    first_staged.extend.assert_called_once_with(final_staged)
    client._inner.commit_staged.assert_called_once_with(
        "models",
        first_staged,
        None,
        _COMMIT_MAX_REBASE_RETRIES,
        None,
    )
    assert progress == [(2, 3), (3, 6)]


def test_load_dataset_rejects_empty_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.return_value = _get_volume_response()
    client._rpc.stream_dataset_download_ur_is.return_value = []

    with pytest.raises(VolumeError, match="yielded no output files"):
        Volume("models", client=client).load_dataset("revision-1")

    client._inner.stage_remote.assert_not_called()
    client._inner.commit_staged.assert_not_called()


def test_put_file_from_path_passes_path_to_native(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.return_value = _get_volume_response()
    _prepare_upload(client)
    src = tmp_path / "model.bin"
    src.write_bytes(b"weights")

    Volume("models", client=client).put_file_from_path("model.bin", src)

    assert client._inner.upload.call_args.args[1] == [("model.bin", src)]


def test_read_file_delegates_to_native_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.return_value = _get_volume_response()
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
    client._rpc.get_volume.return_value = _get_volume_response()
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
    client._rpc.create_ref.return_value = pb_v2.CreateRefResponse(
        ref=pb_v2.RefInfo(
            name="sandbox-a",
            version_id=7,
            commit_id="base-commit",
            parent_version_id=7,
        )
    )

    fork = Volume("models", client=client).at_version(7).fork("sandbox-a")

    client._rpc.create_ref.assert_called_once_with(
        pb_v2.CreateRefRequest(
            volume=pb_v2.VolumeRef(name="models"),
            name="sandbox-a",
            from_version_id=7,
        )
    )
    assert fork.ref == "sandbox-a"
    assert fork.version == VersionInfo(
        id=7,
        ref="sandbox-a",
        commit_id="base-commit",
    )


def test_forks_can_be_chained(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.return_value = _get_volume_response(version_id=7)
    client._rpc.create_ref.side_effect = [
        pb_v2.CreateRefResponse(ref=pb_v2.RefInfo(name="test-fork", version_id=7)),
        pb_v2.CreateRefResponse(ref=pb_v2.RefInfo(name="chained-fork", version_id=7)),
    ]

    chained = Volume("test", client=client).fork("test-fork").fork("chained-fork")

    assert [call.args[0].name for call in client._rpc.create_ref.call_args_list] == [
        "test-fork",
        "chained-fork",
    ]
    assert chained.ref == "chained-fork"


def test_fork_writes_and_lists_versions_on_its_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.return_value = _get_volume_response(version_id=7)
    client._rpc.create_ref.return_value = pb_v2.CreateRefResponse(
        ref=pb_v2.RefInfo(name="sandbox-a", version_id=7)
    )
    client._rpc.list_volume_versions.return_value = pb_v2.ListVolumeVersionsResponse(
        versions=[_version_proto(8)]
    )
    _prepare_upload(client, ref="sandbox-a", version_id=8)
    fork = Volume("models", client=client).fork("sandbox-a")

    fork.put_file("result.txt", b"done")
    versions = fork.versions()

    assert client._inner.upload.call_args.args[-1] == "sandbox-a"
    versions_request = client._rpc.list_volume_versions.call_args.args[0]
    assert versions_request.ref == "sandbox-a"
    assert [version.id for version in versions] == [8]


def test_forks_and_delete_fork_delegate_to_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.return_value = _get_volume_response()
    client._rpc.list_refs.return_value = pb_v2.ListRefsResponse(
        refs=[
            pb_v2.RefInfo(name="main", version_id=10),
            pb_v2.RefInfo(name="sandbox-a", version_id=7, parent_version_id=4),
        ]
    )
    client._rpc.delete_ref.return_value = pb_v2.DeleteRefResponse()
    volume = Volume("models", client=client, ref="sandbox-a")

    assert volume.forks() == [
        ForkInfo(
            name="sandbox-a",
            version_id=7,
            parent_version_id=4,
        )
    ]
    volume.delete_fork()

    client._rpc.list_refs.assert_called_once()
    client._rpc.delete_ref.assert_called_once()


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

    client._rpc.get_volume.assert_not_called()
    client._inner.upload.assert_not_called()


def test_listdir_recurses_through_typed_rpc_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.return_value = _get_volume_response()
    client._rpc.list_files.side_effect = [
        pb_v2.ListFilesResponse(
            files=[
                pb_v2.FileInfo(path="dir", size=0, kind=pb_v2.FileKind.DIRECTORY),
                pb_v2.FileInfo(path="root.txt", size=4, kind=pb_v2.FileKind.FILE),
            ],
            version=_version_proto(),
        ),
        pb_v2.ListFilesResponse(
            files=[
                pb_v2.FileInfo(path="dir/file.txt", size=2, kind=pb_v2.FileKind.FILE)
            ]
        ),
    ]

    files = Volume("models", client=client).listdir()

    assert [f.path for f in files] == ["dir", "root.txt", "dir/file.txt"]


def test_versions_traverse_typed_rpc_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.return_value = _get_volume_response()
    client._rpc.list_volume_versions.side_effect = [
        pb_v2.ListVolumeVersionsResponse(
            versions=[_version_proto(1)], next_cursor="next"
        ),
        pb_v2.ListVolumeVersionsResponse(versions=[_version_proto(2)]),
    ]

    versions = Volume("models", client=client).versions(limit=1)

    assert [v.id for v in versions] == [1, 2]


def test_delete_delegates_to_typed_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.return_value = _get_volume_response()
    client._rpc.delete_volume.return_value = pb_v2.DeleteVolumeResponse()

    Volume("models", client=client).delete()

    client._rpc.delete_volume.assert_called_once_with(
        pb_v2.DeleteVolumeRequest(volume=pb_v2.VolumeRef(name="models"))
    )


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
    created_at_ms = 1_700_000_000_123
    assert vv._dict_to_commit_status(
        {
            "commit_id": "commit-1",
            "result": "committed",
            "version": {
                "version_id": 11,
                "parent_id": 10,
                "created_at_ms": created_at_ms,
                "ref": "main",
                "commit_id": "commit-11",
            },
            "latest_version": {
                "version_id": 12,
                "ref": "main",
                "commit_id": "commit-12",
            },
            "created_at_ms": created_at_ms,
        }
    ) == CommitStatus(
        commit_id="commit-1",
        result="committed",
        version=VersionInfo(
            id=11,
            parent_id=10,
            created_at=vv._ms_to_datetime(created_at_ms),
            ref="main",
            commit_id="commit-11",
        ),
        latest_version=VersionInfo(id=12, ref="main", commit_id="commit-12"),
        created_at=vv._ms_to_datetime(created_at_ms),
    )


def test_empty_files_skip_native_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)

    assert client._upload("models", []) == []
    client._inner.upload.assert_not_called()


def test_native_commit_error_preserves_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._inner.upload.side_effect = RuntimeError(
        "aborted", "commit retries exhausted"
    )

    with pytest.raises(VolumeError, match="commit retries exhausted") as raised:
        client._upload("models", [("model.bin", b"data")])

    assert raised.value.code == "aborted"


def test_remove_file_delegates_to_native_remove_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    client._rpc.get_volume.return_value = _get_volume_response()
    client._inner.remove_files.return_value = _status_dict()

    Volume("models", client=client).remove_file("old/model.bin")

    client._inner.remove_files.assert_called_once_with(
        "models",
        [("old/model.bin", False)],
        _COMMIT_MAX_REBASE_RETRIES,
        None,
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
    client._rpc.get_volume.side_effect = _not_found()
    client._rpc.create_volume.return_value = pb_v2.CreateVolumeResponse(
        volume=_volume_proto("fn-src-add-app")
    )
    intent = pb_v2.CommitIntent(
        volume=pb_v2.VolumeRef(name="src-vol"), author="chalk:env:agent:a"
    )
    client._inner.staged_intent.return_value = intent.to_binary()

    volumes, intents = client.stage_lazy_files(image, volume_name="fn-src-add")

    client._rpc.create_volume.assert_called_once_with(
        pb_v2.CreateVolumeRequest(
            name="fn-src-add-app", volume_type=pb_v2.VolumeKind.SOURCE_CODE
        )
    )

    staged = client._inner.stage.return_value
    client._inner.stage.assert_called_once_with(
        "fn-src-add-app",
        [("f.txt", Path(str(src)))],
        vv._default_upload_config(),
    )
    client._inner.staged_intent.assert_called_once_with(
        "fn-src-add-app",
        staged,
        None,
        [],
    )

    assert intents == [intent]
    assert len(volumes) == 1
    assert volumes[0].name == "fn-src-add-app"
    assert volumes[0].mount_path == "/app"
    assert volumes[0].to_spec_dict()["type"] == "versioned_chalkfs"
    # Nothing commits client-side: the intent lands via volume_commits.
    client._rpc.commit_version.assert_not_called()


def test_stage_lazy_files_reuses_volume_and_replaces_base_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    image, _src = _lazy_image(tmp_path)
    client = _make_client(monkeypatch)
    client._rpc.get_volume.return_value = _get_volume_response("fn-src-add-app")
    intent = pb_v2.CommitIntent(
        volume=pb_v2.VolumeRef(name="fn-src-add-app"),
        deltas=Oneof(
            "path_deltas",
            pb_v2.PathDeltaList(
                removes=[pb_v2.PathRemoveDelta(path="/", recursive=True)]
            ),
        ),
    )
    client._inner.staged_intent.return_value = intent.to_binary()

    volumes, intents = client.stage_lazy_files(image, volume_name="fn-src-add")

    client._rpc.create_volume.assert_not_called()
    assert volumes[0].name == "fn-src-add-app"
    client._inner.staged_intent.assert_called_once_with(
        "fn-src-add-app",
        client._inner.stage.return_value,
        None,
        [("/", True)],
    )
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
