"""Benchmark VersionedVolume batch uploads against a local chalk-private checkout.

This benchmark copies every file-like entry under the source repository,
including ``.git`` and dotfiles, using the customer-facing VersionedVolume
batch upload API. Symlinks are followed: file symlinks upload the target bytes
at the symlink path, and directory symlinks are walked recursively.

Usage:
    uv run python examples/versioned_volume/benchmark_chalk_private_batch_upload.py
    uv run python examples/versioned_volume/benchmark_chalk_private_batch_upload.py --dry-run
    uv run python examples/versioned_volume/benchmark_chalk_private_batch_upload.py --commit-sizes 25,100,500
    uv run python examples/versioned_volume/benchmark_chalk_private_batch_upload.py --source ~/git/chalk-private --keep-volumes
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chalkcompute import VersionedVolume

DEFAULT_SOURCE = Path("~/git/chalk-private").expanduser()
DEFAULT_COMMIT_SIZES = (1, 10, 25, 50, 100, 200, 500)


@dataclass(frozen=True)
class SourceEntry:
    remote_path: str
    local_path: Path
    size_bytes: int
    kind: str


@dataclass(frozen=True)
class BenchmarkResult:
    commit_size: int
    volume_name: str
    file_count: int
    symlink_count: int
    total_bytes: int
    expected_commits: int
    elapsed_s: float

    @property
    def files_per_s(self) -> float:
        return self.file_count / self.elapsed_s if self.elapsed_s else 0.0

    @property
    def mib_per_s(self) -> float:
        return (
            (self.total_bytes / (1024 * 1024)) / self.elapsed_s
            if self.elapsed_s
            else 0.0
        )


def _remote_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _entry_size(path: Path) -> int:
    return path.stat().st_size


def _collect_entries(root: Path) -> list[SourceEntry]:
    if not root.exists():
        raise FileNotFoundError(f"source repo does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"source is not a directory: {root}")

    entries: list[SourceEntry] = []
    unsupported: list[str] = []

    def walk(directory: Path, ancestors: set[Path] | None = None) -> None:
        ancestors = set() if ancestors is None else set(ancestors)
        resolved = directory.resolve()
        if resolved in ancestors:
            unsupported.append(f"{_remote_path(root, directory)} (symlink cycle)")
            return
        ancestors.add(resolved)

        with os.scandir(directory) as iterator:
            for item in sorted(iterator, key=lambda entry: entry.name):
                path = Path(item.path)
                rel = _remote_path(root, path)
                if item.is_symlink():
                    if item.is_dir(follow_symlinks=True):
                        walk(path, ancestors)
                    elif item.is_file(follow_symlinks=True):
                        entries.append(
                            SourceEntry(
                                remote_path=rel,
                                local_path=path,
                                size_bytes=_entry_size(path),
                                kind="symlink",
                            )
                        )
                    else:
                        unsupported.append(rel)
                elif item.is_file(follow_symlinks=False):
                    entries.append(
                        SourceEntry(
                            remote_path=rel,
                            local_path=path,
                            size_bytes=_entry_size(path),
                            kind="file",
                        )
                    )
                elif item.is_dir(follow_symlinks=False):
                    walk(path, ancestors)
                else:
                    unsupported.append(rel)

    walk(root)

    if unsupported:
        sample = ", ".join(unsupported[:10])
        extra = "" if len(unsupported) <= 10 else f", and {len(unsupported) - 10} more"
        raise RuntimeError(
            "source contains filesystem entries that VersionedVolume.put_file "
            f"cannot upload: {sample}{extra}"
        )

    return entries


def _parse_commit_sizes(value: str) -> tuple[int, ...]:
    sizes = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    if not sizes:
        raise argparse.ArgumentTypeError("at least one commit size is required")
    invalid = [size for size in sizes if size <= 0]
    if invalid:
        raise argparse.ArgumentTypeError(f"commit sizes must be positive: {invalid}")
    return sizes


def _upload_once(
    *,
    volume_name: str,
    entries: list[SourceEntry],
    commit_size: int,
) -> BenchmarkResult:
    start = time.perf_counter()
    with VersionedVolume(volume_name) as volume:
        with volume.batch_upload(max_files_per_commit=commit_size) as batch:
            for entry in entries:
                batch.put_file(entry.remote_path, entry.local_path)
    elapsed = time.perf_counter() - start
    return BenchmarkResult(
        commit_size=commit_size,
        volume_name=volume_name,
        file_count=len(entries),
        symlink_count=sum(1 for entry in entries if entry.kind == "symlink"),
        total_bytes=sum(entry.size_bytes for entry in entries),
        expected_commits=math.ceil(len(entries) / commit_size) if entries else 0,
        elapsed_s=elapsed,
    )


def _delete_volume(volume_name: str) -> None:
    try:
        with VersionedVolume(volume_name, create_if_missing=False) as volume:
            volume.delete()
    except Exception as exc:
        print(f"warning: failed to delete {volume_name}: {exc}", file=sys.stderr)


def _print_results(results: list[BenchmarkResult]) -> None:
    print()
    print(
        f"{'commit size':>11} {'commits':>8} {'files':>9} {'symlinks':>8} "
        f"{'MiB':>10} {'seconds':>10} {'files/s':>10} {'MiB/s':>10} volume"
    )
    for item in results:
        print(
            f"{item.commit_size:11d} "
            f"{item.expected_commits:8d} "
            f"{item.file_count:9d} "
            f"{item.symlink_count:8d} "
            f"{item.total_bytes / (1024 * 1024):10.1f} "
            f"{item.elapsed_s:10.3f} "
            f"{item.files_per_s:10.1f} "
            f"{item.mib_per_s:10.1f} "
            f"{item.volume_name}"
        )


def _write_csv(path: Path, results: list[BenchmarkResult]) -> None:
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "commit_size",
                "volume_name",
                "file_count",
                "symlink_count",
                "total_bytes",
                "expected_commits",
                "elapsed_s",
                "files_per_s",
                "mib_per_s",
            ],
        )
        writer.writeheader()
        for result in results:
            row = asdict(result)
            row["files_per_s"] = result.files_per_s
            row["mib_per_s"] = result.mib_per_s
            writer.writerow(row)


def _write_json(path: Path, results: list[BenchmarkResult]) -> None:
    rows = []
    for result in results:
        row = asdict(result)
        row["files_per_s"] = result.files_per_s
        row["mib_per_s"] = result.mib_per_s
        rows.append(row)
    path.write_text(json.dumps(rows, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="Local repo to upload. Defaults to ~/git/chalk-private.",
    )
    parser.add_argument(
        "--commit-sizes",
        type=_parse_commit_sizes,
        default=DEFAULT_COMMIT_SIZES,
        help="Comma-separated max_files_per_commit values to benchmark.",
    )
    parser.add_argument(
        "--volume-prefix",
        default="bench-chalk-private",
        help="Prefix for temporary VersionedVolume names.",
    )
    parser.add_argument(
        "--keep-volumes",
        action="store_true",
        help="Keep benchmark volumes instead of deleting them after upload.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover source entries and exit without uploading.",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        help="Optional path to write CSV results.",
    )
    parser.add_argument(
        "--json",
        type=Path,
        help="Optional path to write JSON results.",
    )
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    entries = _collect_entries(source)
    total_bytes = sum(entry.size_bytes for entry in entries)
    symlink_count = sum(1 for entry in entries if entry.kind == "symlink")
    run_id = uuid.uuid4().hex[:8]

    print(f"Source: {source}")
    print(
        f"Discovered {len(entries)} upload entries "
        f"({symlink_count} symlinks, {total_bytes / (1024 * 1024):.1f} MiB)."
    )
    print(f"Commit sizes: {', '.join(str(size) for size in args.commit_sizes)}")

    if args.dry_run:
        return

    results: list[BenchmarkResult] = []
    created_volumes: list[str] = []
    try:
        for commit_size in args.commit_sizes:
            volume_name = f"{args.volume_prefix}-{run_id}-{commit_size}"
            created_volumes.append(volume_name)
            print(f"\nUploading to {volume_name} with commit size {commit_size}...")
            result = _upload_once(
                volume_name=volume_name,
                entries=entries,
                commit_size=commit_size,
            )
            results.append(result)
            print(
                f"Finished {volume_name}: {result.elapsed_s:.3f}s, "
                f"{result.files_per_s:.1f} files/s, {result.mib_per_s:.1f} MiB/s"
            )
    finally:
        if not args.keep_volumes:
            for volume_name in created_volumes:
                _delete_volume(volume_name)

    _print_results(results)

    if args.csv:
        _write_csv(args.csv, results)
        print(f"\nWrote CSV results to {args.csv}")
    if args.json:
        _write_json(args.json, results)
        print(f"Wrote JSON results to {args.json}")


if __name__ == "__main__":
    main()
