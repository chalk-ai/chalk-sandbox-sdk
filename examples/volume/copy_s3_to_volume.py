# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3", "chalkcompute"]
# ///
"""Copy object-store bucket contents into a Volume.

Works with AWS S3 and any S3-compatible provider (Google Cloud Storage,
Cloudflare R2, MinIO, etc).  Downloads objects concurrently to a temporary
directory, then uploads each chunk as a single commit.  The native
Rust client handles chunking and concurrent uploads internally.

Disk usage is bounded to roughly `max_files_per_commit x avg_object_size`
at any point: each batch is downloaded, committed, and cleaned up before
the next batch begins.

AWS S3 (credentials from env / ~/.aws/credentials / instance profile):
    uv run python examples/volume/copy_s3_to_volume.py \\
        --bucket my-bucket --volume my-volume
    uv run python examples/volume/copy_s3_to_volume.py \\
        --bucket my-bucket --prefix data/2024/ \\
        --volume my-volume --dest-prefix imported/
    uv run python examples/volume/copy_s3_to_volume.py \\
        --bucket my-bucket --volume my-volume --dry-run

Google Cloud Storage via the S3-compatible XML API (requires HMAC keys):
    export AWS_ACCESS_KEY_ID=$GOOGLE_ACCESS_KEY_ID
    export AWS_SECRET_ACCESS_KEY=$GOOGLE_ACCESS_KEY_SECRET
    uv run python examples/volume/copy_s3_to_volume.py \\
        --bucket my-gcs-bucket --volume my-volume \\
        --endpoint-url https://storage.googleapis.com
"""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3

from chalkcompute import Volume

log = logging.getLogger(__name__)

_DEFAULT_MAX_DOWNLOAD_WORKERS = 32
_DEFAULT_MAX_FILES_PER_COMMIT = 200

_GCS_ENDPOINT = "https://storage.googleapis.com"


def _build_s3_client(
    *,
    endpoint_url: str | None = None,
    access_key_id: str | None = None,
    secret_access_key: str | None = None,
    region_name: str | None = None,
) -> Any:
    """Return a boto3 S3 client, optionally aimed at an S3-compatible endpoint."""
    kwargs: dict = {}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    if access_key_id:
        kwargs["aws_access_key_id"] = access_key_id
    if secret_access_key:
        kwargs["aws_secret_access_key"] = secret_access_key
    if region_name:
        kwargs["region_name"] = region_name
    return boto3.client("s3", **kwargs)


@dataclass(frozen=True)
class _S3Object:
    key: str
    size_bytes: int
    dest_path: str


def _list_objects(s3_client: Any, bucket: str, prefix: str) -> list[_S3Object]:
    paginator = s3_client.get_paginator("list_objects_v2")
    objects: list[_S3Object] = []
    kwargs: dict = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix
    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            key: str = obj["Key"]
            rel = key[len(prefix) :].lstrip("/") if prefix else key
            if not rel:
                continue
            objects.append(_S3Object(key=key, size_bytes=obj["Size"], dest_path=rel))
    return objects


def _download_object(
    s3_client: Any,
    bucket: str,
    obj: _S3Object,
    tmp_dir: str,
) -> tuple[_S3Object, Path]:
    local_path = Path(tmp_dir) / obj.dest_path.replace("/", os.sep)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    s3_client.download_file(bucket, obj.key, str(local_path))
    return obj, local_path


def copy_s3_to_volume(
    *,
    s3_client: Any,
    bucket: str,
    prefix: str,
    volume: Volume,
    dest_prefix: str = "",
    max_download_workers: int = _DEFAULT_MAX_DOWNLOAD_WORKERS,
    max_files_per_commit: int = _DEFAULT_MAX_FILES_PER_COMMIT,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Copy objects from ``s3://bucket/prefix`` into ``volume`` at ``dest_prefix``.

    Returns ``(file_count, total_bytes)`` for the objects processed.
    """
    log.info("Listing objects in s3://%s/%s", bucket, prefix)
    raw_objects = _list_objects(s3_client, bucket, prefix)

    norm_dest = dest_prefix.strip("/") + "/" if dest_prefix else ""
    objects = [
        _S3Object(key=o.key, size_bytes=o.size_bytes, dest_path=norm_dest + o.dest_path)
        for o in raw_objects
    ]

    total_bytes = sum(o.size_bytes for o in objects)
    print(f"Found {len(objects)} objects ({total_bytes / (1024 * 1024):.1f} MiB)")

    if dry_run or not objects:
        return len(objects), total_bytes

    with tempfile.TemporaryDirectory() as tmp_dir:
        log.info(
            "Downloading %d objects with %d workers", len(objects), max_download_workers
        )
        downloaded: list[tuple[_S3Object, Path]] = []
        with ThreadPoolExecutor(max_workers=max_download_workers) as executor:
            futures = {
                executor.submit(_download_object, s3_client, bucket, obj, tmp_dir): obj
                for obj in objects
            }
            for future in as_completed(futures):
                result = future.result()
                downloaded.append(result)
                log.debug("Downloaded %s", result[0].key)

        with volume.batch_upload(max_files_per_commit=max_files_per_commit) as batch:
            for obj, local_path in downloaded:
                batch.put_file(obj.dest_path, local_path)

    return len(objects), total_bytes


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--bucket", required=True, help="Source S3 bucket name.")
    parser.add_argument(
        "--prefix",
        default="",
        help="Source key prefix to copy (default: all objects).",
    )
    parser.add_argument(
        "--volume",
        required=True,
        help="Destination Volume name (created if absent).",
    )
    parser.add_argument(
        "--dest-prefix",
        default="",
        help="Path prefix to prepend to all destination paths in the volume.",
    )
    parser.add_argument(
        "--max-download-workers",
        type=int,
        default=_DEFAULT_MAX_DOWNLOAD_WORKERS,
        help=f"Concurrent S3 download threads per chunk (default: {_DEFAULT_MAX_DOWNLOAD_WORKERS}).",
    )
    parser.add_argument(
        "--max-files-per-commit",
        type=int,
        default=_DEFAULT_MAX_FILES_PER_COMMIT,
        help=f"Files per volume commit (default: {_DEFAULT_MAX_FILES_PER_COMMIT}).",
    )
    parser.add_argument(
        "--endpoint-url",
        default=None,
        metavar="URL",
        help=(
            "S3-compatible endpoint URL. "
            f"Use {_GCS_ENDPOINT!r} for Google Cloud Storage."
        ),
    )
    parser.add_argument(
        "--access-key-id",
        default=None,
        help="Override AWS_ACCESS_KEY_ID (useful for GCS HMAC keys).",
    )
    parser.add_argument(
        "--secret-access-key",
        default=None,
        help="Override AWS_SECRET_ACCESS_KEY (useful for GCS HMAC secrets).",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="Override the AWS region sent with requests.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List objects only; do not download or upload.",
    )
    args = parser.parse_args()

    s3 = _build_s3_client(
        endpoint_url=args.endpoint_url,
        access_key_id=args.access_key_id,
        secret_access_key=args.secret_access_key,
        region_name=args.region,
    )

    endpoint_label = f" ({args.endpoint_url})" if args.endpoint_url else ""
    source = (
        f"s3://{args.bucket}/{args.prefix}" if args.prefix else f"s3://{args.bucket}"
    ) + endpoint_label
    dest = f"{args.volume!r}"
    if args.dest_prefix:
        dest += f" at {args.dest_prefix!r}"
    print(f"Source:      {source}")
    print(f"Destination: {dest}")
    if args.dry_run:
        print("Dry run: listing only, no uploads.")

    start = time.perf_counter()
    with Volume(args.volume) as volume:
        file_count, total_bytes = copy_s3_to_volume(
            s3_client=s3,
            bucket=args.bucket,
            prefix=args.prefix,
            volume=volume,
            dest_prefix=args.dest_prefix,
            max_download_workers=args.max_download_workers,
            max_files_per_commit=args.max_files_per_commit,
            dry_run=args.dry_run,
        )

    elapsed = time.perf_counter() - start
    mib = total_bytes / (1024 * 1024)
    rate_files = file_count / elapsed if elapsed else 0.0
    rate_mib = mib / elapsed if elapsed else 0.0
    print(
        f"\nDone: {file_count} files, {mib:.1f} MiB "
        f"in {elapsed:.3f}s ({rate_files:.1f} files/s, {rate_mib:.1f} MiB/s)"
    )


if __name__ == "__main__":
    main()
