"""End-to-end tests for the v2 Volume Python SDK."""

from __future__ import annotations

from collections.abc import Iterator
import uuid
import warnings

import pytest

from chalkcompute import (
    ConnectClient,
    Volume,
    VolumeClient,
    VolumeError,
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
