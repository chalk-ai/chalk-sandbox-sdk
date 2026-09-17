"""End-to-end tests for the v2 Volume Python SDK."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import uuid
import warnings
from pathlib import Path

import pytest

from chalkcompute import (
    ConnectClient,
    DatasetClient,
    Volume,
    VolumeClient,
    VolumeError,
    VolumeFileNotFoundError,
)
from e2etests.conftest import needs_staging
from chalkcompute._gen.chalk.server.v1 import datasets_connect, datasets_pb

MODEL_PATH = "models/latest.bin"
TRAIN_PATH = "data/train.csv"
EVAL_PATH = "data/eval.csv"
LOCAL_NOTES_PATH = "notes/from-local.txt"

pytestmark = needs_staging


@pytest.fixture
def volume() -> Iterator[Volume]:
    volume = Volume(f"a-test-volume-{uuid.uuid4().hex[:8]}")
    try:
        yield volume
    finally:
        try:
            volume.delete()
        except VolumeError:
            pass
        volume.close()


def test_put_and_read_file(volume: Volume) -> None:
    model_bytes = b"pretend model weights\n"

    volume.put_file(MODEL_PATH, model_bytes)

    assert volume.read_file(MODEL_PATH) == model_bytes


@pytest.mark.parametrize("kind", ["sandbox", "container"])
def test_uploaded_files_are_readable_immediately_after_startup(
    tmp_path: Path, kind: str
) -> None:
    from chalkcompute import Container, Image, Sandbox

    assets = tmp_path / "data.txt"
    assets.write_text("uploaded assets")
    config = tmp_path / "settings.txt"
    config.write_text("uploaded settings")
    image = (
        Image.base("python:3.11-slim")
        .add_local_file(str(assets), "/assets/data.txt")
        .add_local_file(str(config), "/config/settings.txt")
    )
    resource = (
        Sandbox(image=image, lifetime="300s")
        if kind == "sandbox"
        else Container(image=image, lifetime="300s")
    )
    with ExitStack() as cleanup:
        cleanup.callback(
            resource.terminate if isinstance(resource, Sandbox) else resource.stop
        )
        resource.run()
        result = resource.exec(
            "python3",
            "-c",
            "from pathlib import Path\n"
            "print(Path('/assets/data.txt').read_text())\n"
            "print(Path('/config/settings.txt').read_text())\n",
            timeout_secs=30,
        )
        assert result.exit_code == 0, result.stderr_text
        assert result.stdout_text.splitlines() == [
            "uploaded assets",
            "uploaded settings",
        ]


def test_resolved_content_pins_and_separate_saves(
    volume: Volume, tmp_path: Path
) -> None:
    from chalkcompute import Image, Sandbox

    (tmp_path / "bridge.py").write_text("old bridge")
    volume.put_dir(tmp_path)
    old = Image.base("python:3.11-slim").build(volumes=[volume.mount("/assets")])
    (tmp_path / "bridge.py").write_text("new bridge")
    volume.put_dir(tmp_path)
    new = Image.base("python:3.11-slim").build(volumes=[volume.mount("/assets")])
    assert old.volumes[0].version_id != new.volumes[0].version_id

    with ExitStack() as resources:
        client = resources.enter_context(VolumeClient.from_env())
        for spec, content, player in [
            (old, "old bridge", "alice"),
            (new, "new bridge", "bob"),
        ]:
            saves = client.create(f"a-test-saves-{uuid.uuid4().hex[:12]}")
            resources.callback(saves.delete)
            saves.put_file("player.txt", player)
            sandbox = Sandbox(
                image=Image.from_json(spec.to_json()),
                volumes=[saves.mount("/saves")],
                lifetime="300s",
            )
            resources.callback(sandbox.terminate)
            sandbox.run()
            result = sandbox.exec(
                "python3",
                "-c",
                "from pathlib import Path; "
                "print(Path('/assets/bridge.py').read_text()); "
                "print(Path('/saves/player.txt').read_text()); "
                "print(','.join(sorted(p.name for p in Path('/saves').iterdir() "
                "if not p.name.startswith('.'))))",
                timeout_secs=30,
            )
            assert result.exit_code == 0
            assert result.stdout_text.strip().splitlines() == [
                content,
                player,
                "player.txt",
            ]
            sandbox.terminate()
            assert client.exists(volume.name)
            assert client.exists(saves.name)
        assert (
            volume.at_version(old.volumes[0].version_id).read_file("bridge.py")
            == b"old bridge"
        )


def test_resolved_image_reuses_source_volume(tmp_path: Path) -> None:
    from chalkcompute import Image, VolumeClient
    from chalkcompute._image_build import default_source_volume_name

    source = tmp_path / "bridge.py"
    source.write_text("old bridge")
    run_id = uuid.uuid4().hex[:12]
    image = (
        Image.base("python:3.11-slim")
        .env({"CHALK_E2E_RUN_ID": run_id})
        .add_local_file(str(source), "/app/bridge.py")
    )
    explicit_name = f"e2e-resolved-src-{run_id}"
    default_name = default_source_volume_name(image)
    try:
        old = image.build(name=explicit_name)
        source.write_text("new bridge")
        new = image.build(name=explicit_name)

        assert old.volumes[0].name == explicit_name
        assert new.volumes[0].name == explicit_name
        assert old.volumes[0].version_id != new.volumes[0].version_id

        unnamed_old = image.build()
        source.write_text("new unnamed bridge")
        unnamed_new = image.build()
        assert (
            unnamed_old.volumes[0].name == unnamed_new.volumes[0].name == default_name
        )
        assert unnamed_old.volumes[0].version_id != unnamed_new.volumes[0].version_id
    finally:
        with VolumeClient.from_env() as client:
            with ExitStack() as cleanup:
                for name in (explicit_name, default_name):
                    if client.exists(name):
                        cleanup.callback(client.delete_volume, name)


def test_batch_upload_and_listdir(volume: Volume) -> None:
    volume.put_file(MODEL_PATH, b"base version\n")

    with volume.batch_upload() as batch:
        batch.put(TRAIN_PATH, b"id,label\n1,approved\n2,declined\n")
        batch.put(EVAL_PATH, b"id,label\n3,approved\n")

    data_paths = {item.path for item in volume.listdir("data")}
    assert {TRAIN_PATH, EVAL_PATH}.issubset(data_paths)


def test_put_file_from_path(volume: Volume, tmp_path) -> None:
    local_notes = "uploaded from a local path\n"
    local_path = tmp_path / "notes.txt"
    local_path.write_text(local_notes)

    volume.put_file_from_path(LOCAL_NOTES_PATH, local_path)

    assert volume.read_file(LOCAL_NOTES_PATH) == local_notes.encode()


@pytest.mark.parametrize("file_format", ["csv", "parquet"])
@pytest.mark.parametrize("multiple", [False, True], ids=["single", "multiple"])
def test_load_dataset_round_trip(
    volume: Volume, tmp_path: Path, file_format: str, multiple: bool
) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    source_paths = []
    expected_rows = []
    for index in range(2 if multiple else 1):
        source_path = tmp_path / f"source-{index}.{file_format}"
        rows = [
            {"id": 2 * index + 1, "label": "approved"},
            {"id": 2 * index + 2, "label": "declined"},
        ]
        if file_format == "parquet":
            pq.write_table(pa.Table.from_pylist(rows), source_path)
        else:
            source_path.write_text(
                "id,label\n" + "".join(f"{row['id']},{row['label']}\n" for row in rows)
            )
        source_paths.append(source_path)
        expected_rows.extend(rows)

    connect = ConnectClient()
    revision = DatasetClient(connect_client=connect).upload(
        f"a-test-dataset-{uuid.uuid4().hex[:8]}",
        tmp_path.glob(f"*.{file_format}") if multiple else source_paths[0],
    )
    try:
        first = volume.load_dataset(revision, dest_prefix="training")
        parquet_files = [
            item
            for item in volume.listdir("training")
            if item.kind == "file" and item.path.endswith(".parquet")
        ]

        assert len(parquet_files) == len(source_paths)
        uploaded = {item.path: volume.read_file(item.path) for item in parquet_files}
        actual_rows = [
            row
            for content in uploaded.values()
            for row in pq.read_table(pa.BufferReader(content)).to_pylist()
        ]
        assert sorted(actual_rows, key=lambda row: row["id"]) == expected_rows
        if file_format == "parquet":
            assert sorted(uploaded.values()) == sorted(
                path.read_bytes() for path in source_paths
            )

        second = volume.load_dataset(revision, dest_prefix="training")

        assert second.id != first.id
        assert {path: volume.read_file(path) for path in uploaded} == uploaded
    finally:
        try:
            connect.call(
                connect.stub(
                    datasets_connect.DatasetMetadataServiceClientSync
                ).delete_dataset,
                datasets_pb.DeleteDatasetRequest(dataset_id=revision.dataset_id),
            )
        except Exception as exc:
            warnings.warn(
                f"Failed to clean up test dataset {revision.dataset_id}: {exc}"
            )


def test_versions_include_committed_writes(volume: Volume) -> None:
    volume.put_file("one.txt", b"one")
    first = volume.latest_version()
    volume.put_file("two.txt", b"two")
    second = volume.latest_version()

    versions = volume.versions(limit=1)

    version_ids = {version.id for version in versions}
    assert {first.id, second.id}.issubset(version_ids)
    assert second.id != first.id


def test_pinned_version_can_read_removed_file(volume: Volume) -> None:
    eval_csv = b"id,label\n3,approved\n"
    volume.put_file(EVAL_PATH, eval_csv)
    before_remove = volume.latest_version()

    volume.remove_file(EVAL_PATH)
    after_remove = volume.latest_version()

    assert after_remove.id != before_remove.id
    assert volume.at_version(before_remove.id).read_file(EVAL_PATH) == eval_csv


def test_pinned_version_rejects_writes(volume: Volume) -> None:
    volume.put_file(MODEL_PATH, b"base version\n")

    pinned = volume.at_version(volume.latest_version().id)

    with pytest.raises(VolumeError, match="read-only"):
        pinned.put_file("data/should-fail.txt", b"immutable")


def test_fork_diverges_from_main(volume: Volume) -> None:
    volume.put_file("shared.txt", b"base")
    base = volume.latest_version()
    fork_name = f"test-{uuid.uuid4().hex[:8]}"
    try:
        fork = volume.at_version(base.id).fork(fork_name)
    except VolumeError as error:
        # forks need route_volume_to_dataplane, which meta-ci does not enable
        if error.code == "unimplemented" or "route_volume_to_dataplane" in str(error):
            warnings.warn(
                f"forks unavailable in this environment: {error}", stacklevel=1
            )
            pytest.skip(str(error))
        raise
    try:
        fork.put_file("fork-only.txt", b"fork")

        assert fork.read_file("shared.txt") == b"base"
        assert fork.read_file("fork-only.txt") == b"fork"
        assert "fork-only.txt" not in {item.path for item in volume.listdir()}
        assert any(ref.name == fork_name for ref in volume.forks())
    finally:
        fork.delete_fork()


def test_concurrent_fork_write_consistency(volume: Volume) -> None:
    """Independent fork writers persist readable immutable versions."""
    score_path = "scores/value.txt"
    writer_a_ref = "writer-a"
    writer_b_ref = "writer-b"
    winner_ref = "winner"

    try:
        base_score = int(volume.read_file(score_path))
    except VolumeFileNotFoundError:
        base_score = 0
        volume.put_file(score_path, str(base_score))
    base = volume.latest_version()

    try:
        refs = {ref.name for ref in volume.forks()}

        def open_or_create_fork(name: str) -> Volume:
            if name in refs:
                return volume.at_ref(name)
            try:
                return volume.at_version(base.id).fork(name)
            except VolumeError as error:
                if error.code == "already_exists" or "already exists" in str(error):
                    return volume.at_ref(name)
                raise

        writer_a = open_or_create_fork(writer_a_ref)
        writer_b = open_or_create_fork(writer_b_ref)
        winner = open_or_create_fork(winner_ref)
    except VolumeError as error:
        if error.code == "unimplemented" or "route_volume_to_dataplane" in str(error):
            pytest.skip(f"forks unavailable in this environment: {error}")
        raise

    score_a = base_score + 1
    score_b = base_score + 2

    def write_score(fork: Volume, score: int) -> tuple[int, int]:
        fork.put_file(score_path, str(score))
        return score, fork.latest_version().id

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(
            lambda args: write_score(*args),
            ((writer_a, score_a), (writer_b, score_b)),
        )

    for fork, (score, version_id) in zip((writer_a, writer_b), (first, second)):
        assert version_id > base.id
        assert fork.at_version(version_id).read_file(score_path) == str(score).encode()

    winning_score = max(score_a, score_b)
    winner.put_file(score_path, str(winning_score))
    winner_version = winner.latest_version()

    assert winner_version.id > base.id
    assert (
        winner.at_version(winner_version.id).read_file(score_path)
        == str(winning_score).encode()
    )


def test_client_lookup_helpers(volume: Volume) -> None:
    volume.put_file(MODEL_PATH, b"base version\n")

    with VolumeClient.from_connect(ConnectClient()) as client:
        assert client.from_name(volume.name).name == volume.name
        looked_up = client.lookup(volume.name)
        assert looked_up.info.name == volume.name
        assert client.exists(volume.name)


def test_documented_round_trip(volume: Volume) -> None:
    model_bytes = b"pretend model weights\n"
    train_csv = b"id,label\n1,approved\n2,declined\n"
    eval_csv = b"id,label\n3,approved\n"

    volume.put_file(MODEL_PATH, model_bytes)
    with volume.batch_upload() as batch:
        batch.put(TRAIN_PATH, train_csv)
        batch.put(EVAL_PATH, eval_csv)

    assert volume.read_file(MODEL_PATH) == model_bytes
    assert volume.read_file(TRAIN_PATH) == train_csv
    assert volume.read_file(EVAL_PATH) == eval_csv

    data_paths = {item.path for item in volume.listdir("data")}
    assert {TRAIN_PATH, EVAL_PATH}.issubset(data_paths)
