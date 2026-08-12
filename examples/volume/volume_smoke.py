"""Volume SDK example.

This mirrors the Python SDK flow from the volume documentation, using
Volume instead of Volume:

1. Open a named volume with ``Volume("my-data")``.
2. Upload files directly and with ``batch_upload``.
3. Read files and list directories.
4. Inspect versions and read a previous immutable snapshot.

Volumes are not mountable into containers or sandboxes yet, so this
example intentionally uses only the direct Volume Python APIs.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from pathlib import Path

from chalkcompute import (
    ConnectClient,
    Volume,
    VolumeClient,
    VolumeError,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

MODEL_PATH = "models/latest.bin"
TRAIN_PATH = "data/train.csv"
EVAL_PATH = "data/eval.csv"
LOCAL_NOTES_PATH = "notes/from-local.txt"


def _print_listing(volume: Volume, prefix: str = "") -> None:
    print(f"\nFiles under {prefix or '/'}:")
    for item in volume.listdir(prefix):
        kind = f" {item.kind}" if item.kind else ""
        print(f"  - {item.path} ({item.size} bytes{kind})")


def _print_versions(volume: Volume) -> None:
    print("\nVersions:")
    for version in volume.versions():
        print(f"  - id={version.id} ref={version.ref or '<none>'}")


def main() -> None:
    volume_name = os.environ.get(
        "VERSIONED_VOLUME_NAME",
        f"volume-example-{uuid.uuid4().hex[:8]}",
    )
    keep_volume = os.environ.get("KEEP_VERSIONED_VOLUME") == "1"

    print(f"Volume: {volume_name}")
    print("Opening with lazy get-or-create semantics...")

    with Volume(volume_name) as volume:
        try:
            # This follows the documented Volume shape:
            #
            #   vol = Volume("my-data")
            #   vol.put_file("models/latest.bin", model_bytes)
            #   with vol.batch_upload() as batch:
            #       batch.put("data/train.csv", train_csv)
            #       batch.put("data/eval.csv", eval_csv)
            #   data = vol.read_file("models/latest.bin")
            #   for f in vol.listdir("data/"):
            #       print(f.path, f.size)

            model_bytes = b"pretend model weights\n"
            train_csv = b"id,label\n1,approved\n2,declined\n"
            eval_csv = b"id,label\n3,approved\n"

            log.info("Uploading %s", MODEL_PATH)
            volume.put_file(MODEL_PATH, model_bytes)

            log.info("Batch uploading training data")
            with volume.batch_upload() as batch:
                batch.put(TRAIN_PATH, train_csv)
                batch.put(EVAL_PATH, eval_csv)

            data = volume.read_file(MODEL_PATH)
            assert data == model_bytes
            print(f"\nRead {MODEL_PATH}: {data.decode().strip()!r}")
            _print_listing(volume, "data")

            # Additional Volume SDK functionality.

            with tempfile.TemporaryDirectory() as tmpdir:
                local_path = Path(tmpdir) / "notes.txt"
                local_path.write_text("uploaded from a local path\n")
                volume.put_file_from_path(LOCAL_NOTES_PATH, local_path)

            before_remove = volume.latest_version()
            print(f"Snapshot before removal: {before_remove.id}")

            volume.remove_file(EVAL_PATH)
            after_remove = volume.latest_version()
            print(f"Snapshot after removal: {after_remove.id}")

            previous = volume.at_version(before_remove.id)
            previous_eval = previous.read_file(EVAL_PATH)
            assert previous_eval == eval_csv
            print(
                f"Read deleted file from previous version: {previous_eval.decode().strip()!r}"
            )

            try:
                previous.put_file("data/should-fail.txt", b"immutable")
            except VolumeError as exc:
                print(f"Previous-version write rejected: {exc}")
            else:
                raise AssertionError("at_version handles must be read-only")

            _print_versions(volume)
            _print_listing(volume)

            # Client helper ergonomics. These use the volume client
            # only; no v1 VolumeClient, containers, or sandboxes are involved.
            with VolumeClient.from_connect(ConnectClient()) as client:
                known_existing = client.from_name(volume_name)
                print(f"\nfrom_name handle: {known_existing.name}")

                looked_up = client.lookup(volume_name)
                print(
                    f"lookup info: name={looked_up.info.name} volume_id={looked_up.info.volume_id}"
                )

        finally:
            if keep_volume:
                print(
                    f"\nKeeping volume because KEEP_VERSIONED_VOLUME=1: {volume_name}"
                )
            else:
                log.info("Deleting volume %s", volume_name)
                volume.delete()


if __name__ == "__main__":
    main()
