"""End-to-end tests for the v2 Volume Python SDK."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
import uuid
import warnings

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


def test_load_dataset_round_trip(volume: Volume, tmp_path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    source_path = tmp_path / "source.parquet"
    pq.write_table(
        pa.table({"id": [1, 2], "label": ["approved", "declined"]}),
        source_path,
    )
    source_bytes = source_path.read_bytes()
    revision = DatasetClient().upload(
        f"a-test-dataset-{uuid.uuid4().hex[:8]}",
        source_path,
    )

    first = volume.load_dataset(revision, dest_prefix="training")
    parquet_files = [
        item
        for item in volume.listdir("training")
        if item.kind == "file" and item.path.endswith(".parquet")
    ]

    assert len(parquet_files) == 1
    assert volume.read_file(parquet_files[0].path) == source_bytes

    second = volume.load_dataset(revision, dest_prefix="training")

    assert second.id != first.id
    assert volume.read_file(parquet_files[0].path) == source_bytes


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
