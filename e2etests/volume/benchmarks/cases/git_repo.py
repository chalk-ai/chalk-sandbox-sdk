"""Benchmark Git repository ingestion and status traversal on a Volume."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
import tempfile
import time
from typing import Any

from chalkcompute import (
    Image,
    NetworkPolicy,
    Sandbox,
    Volume,
)
from chalkcompute._git import unpack_remote_to_mount
from e2etests.volume.benchmarks.harness import (
    BenchmarkFailure,
    positive_int,
    read_committed_json,
    run_benchmark,
    run_sandbox_workload,
    temporary_volume,
)

VLLM_URL = "https://github.com/vllm-project/vllm.git"
VLLM_SHA = "2cf0a6915ce544dc493a0990f2ea38d81601128a"
MOUNT_PATH = "/git-repo"
PAYLOADS = Path(__file__).parents[1] / "payloads"
CLONE_SCRIPT_SOURCE = PAYLOADS / "git_repo" / "clone.sh"
CLONE_SCRIPT_DEST = "/opt/chalk/benchmarks/git-clone.sh"
STATUS_SCRIPT_SOURCE = PAYLOADS / "git_repo" / "status.sh"
STATUS_SCRIPT_DEST = "/opt/chalk/benchmarks/git-status.sh"
GIT_COMMIT_SCRIPT_SOURCE = PAYLOADS / "git_repo" / "commit.sh"
GIT_COMMIT_SCRIPT_DEST = "/opt/chalk/benchmarks/git-commit.sh"
COMMIT_SCRIPT_SOURCE = PAYLOADS / "commit.sh"
COMMIT_SCRIPT_DEST = "/opt/chalk/benchmarks/commit-volume.sh"
CLONE_RESULT_PATH = "clone-result.json"
CLONE_LOG_PATH = "clone.log"

CLONE_ENTRYPOINT = """
mkdir -p "$3/vllm"
clone_started_ns=$(date +%s%N)
/bin/sh "$1" "$4" "$5" "$3/vllm" >"$3/$6" 2>&1
clone_status=$?
clone_finished_ns=$(date +%s%N)
repo_file_count=$(find "$3/vllm" -path "$3/vllm/.git" -prune -o -type f -print | wc -l)
commit_started_ns=$(date +%s%N)
/bin/sh "$2" "$3" "$3/$6"
commit_status=$?
commit_finished_ns=$(date +%s%N)
printf '{"clone_elapsed_ns":%s,"commit_elapsed_ns":%s,"repo_file_count":%s}\n' \
  "$((clone_finished_ns - clone_started_ns))" \
  "$((commit_finished_ns - commit_started_ns))" \
  "$repo_file_count" >"$3/$7"
"$3/.chalk-volume/commit"
metrics_commit_status=$?
if [ "$clone_status" -ne 0 ]; then
  exit "$clone_status"
fi
if [ "$commit_status" -ne 0 ]; then
  exit "$commit_status"
fi
exit "$metrics_commit_status"
"""
STATUS_ENTRYPOINT = """
/bin/sh "$1" "$2" "$2/$3" "$2/$4"
benchmark_status=$?
"$2/.chalk-volume/commit" >>"$2/$4" 2>&1
metrics_commit_status=$?
if [ "$benchmark_status" -ne 0 ]; then
  exit "$benchmark_status"
fi
exit "$metrics_commit_status"
"""


@dataclass(frozen=True)
class FuseCloneResult:
    version_id: int
    write_seconds: float
    commit_seconds: float
    repo_file_count: int


@dataclass(frozen=True)
class SdkCloneResult:
    version_id: int
    elapsed_seconds: float
    versions_created: int


@dataclass(frozen=True)
class GitStatusResult:
    version_id: int
    elapsed_seconds: float
    dirty_paths: int


@dataclass(frozen=True)
class GitCommitResult:
    version_id: int
    elapsed_seconds: float
    exit_code: int
    commit_sha: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-url", default=VLLM_URL)
    parser.add_argument("--repo-ref", default=VLLM_SHA)
    parser.add_argument("--cpu", default="2")
    parser.add_argument("--memory", default="4Gi")
    parser.add_argument("--timeout-secs", type=positive_int, default=2700)
    parser.add_argument("--keep-volumes", action="store_true")
    return parser.parse_args()


def _benchmark_image() -> Image:
    return (
        Image.debian_slim(python_version="3.12")
        .run_commands(
            "apt-get update && apt-get install -y --no-install-recommends "
            + "git ca-certificates && rm -rf /var/lib/apt/lists/*"
        )
        .add_local_file(
            str(CLONE_SCRIPT_SOURCE), CLONE_SCRIPT_DEST, mode=0o755, strategy="copy"
        )
        .add_local_file(
            str(STATUS_SCRIPT_SOURCE),
            STATUS_SCRIPT_DEST,
            mode=0o755,
            strategy="copy",
        )
        .add_local_file(
            str(GIT_COMMIT_SCRIPT_SOURCE),
            GIT_COMMIT_SCRIPT_DEST,
            mode=0o755,
            strategy="copy",
        )
        .add_local_file(
            str(COMMIT_SCRIPT_SOURCE),
            COMMIT_SCRIPT_DEST,
            mode=0o755,
            strategy="copy",
        )
    )


def _read_json(
    volume: Volume,
    path: str,
    timeout_secs: int = 120,
    poll_interval: float = 2,
) -> tuple[int, dict[str, Any]]:
    committed = read_committed_json(
        volume,
        {"result": path},
        timeout_secs=timeout_secs,
        poll_interval=poll_interval,
        description=f"Volume result {path!r}",
    )
    return committed.version_id, committed.value["result"]


def _run_fuse_clone(
    volume: Volume, image: Image, args: argparse.Namespace
) -> FuseCloneResult:
    sandbox = Sandbox(
        image=image,
        cpu=args.cpu,
        memory=args.memory,
        network_policy=NetworkPolicy(allowed_hosts=["github.com"]),
        volumes=[volume.mount(MOUNT_PATH)],
        entrypoint=[
            "/bin/sh",
            "-c",
            CLONE_ENTRYPOINT,
            "git-repo-clone",
            CLONE_SCRIPT_DEST,
            COMMIT_SCRIPT_DEST,
            MOUNT_PATH,
            args.repo_url,
            args.repo_ref,
            CLONE_LOG_PATH,
            CLONE_RESULT_PATH,
        ],
    )
    run_sandbox_workload(
        sandbox,
        timeout_secs=args.timeout_secs,
        phase="git repo FUSE clone",
        log=(volume, CLONE_LOG_PATH),
    )

    version_id, result = _read_json(volume, CLONE_RESULT_PATH)
    clone_elapsed_ns = result.get("clone_elapsed_ns")
    commit_elapsed_ns = result.get("commit_elapsed_ns")
    repo_file_count = result.get("repo_file_count")
    if not isinstance(clone_elapsed_ns, int) or clone_elapsed_ns < 0:
        raise ValueError("FUSE clone timing is invalid")
    if not isinstance(commit_elapsed_ns, int) or commit_elapsed_ns < 0:
        raise ValueError("FUSE commit timing is invalid")
    if not isinstance(repo_file_count, int) or repo_file_count < 1:
        raise ValueError("FUSE repository file count is invalid")
    return FuseCloneResult(
        version_id=version_id,
        write_seconds=clone_elapsed_ns / 1_000_000_000,
        commit_seconds=commit_elapsed_ns / 1_000_000_000,
        repo_file_count=repo_file_count,
    )


def _run_sdk_clone(volume: Volume, args: argparse.Namespace) -> SdkCloneResult:
    versions_before = len(volume.versions())
    started = time.monotonic()
    _ = volume.load_git_repo(
        args.repo_url,
        dest_prefix="vllm",
        revision=args.repo_ref,
    )
    elapsed_seconds = time.monotonic() - started
    versions_after = len(volume.versions())
    return SdkCloneResult(
        version_id=volume.latest_version().id,
        elapsed_seconds=elapsed_seconds,
        versions_created=versions_after - versions_before,
    )


def _run_local_clone(args: argparse.Namespace) -> tuple[float, set[str]]:
    with tempfile.TemporaryDirectory(prefix="chalk-volume-benchmark-git-") as temp:
        checkout = Path(temp) / "vllm"
        started = time.monotonic()
        unpack_remote_to_mount(args.repo_url, checkout, args.repo_ref)
        elapsed_seconds = time.monotonic() - started
        paths = {
            path.relative_to(checkout).as_posix()
            for path in checkout.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and ".git" not in path.relative_to(checkout).parts
        }
        symlinks = [path for path in checkout.rglob("*") if path.is_symlink()]
        if symlinks:
            raise ValueError("Local Git checkout unexpectedly contains symlinks")
        return elapsed_seconds, paths


def _run_git_status(
    volume: Volume,
    image: Image,
    args: argparse.Namespace,
    clone_path: str,
) -> GitStatusResult:
    result_path = f"git-status-{clone_path}.json"
    log_path = f"git-status-{clone_path}.log"
    sandbox = Sandbox(
        image=image,
        cpu=args.cpu,
        memory=args.memory,
        volumes=[volume.mount(MOUNT_PATH)],
        entrypoint=[
            "/bin/sh",
            "-c",
            STATUS_ENTRYPOINT,
            f"git-status-{clone_path}",
            STATUS_SCRIPT_DEST,
            MOUNT_PATH,
            result_path,
            log_path,
        ],
    )
    run_sandbox_workload(
        sandbox,
        timeout_secs=args.timeout_secs,
        phase=f"git status on {clone_path} clone",
        log=(volume, log_path),
    )

    version_id, result = _read_json(volume, result_path)
    elapsed_ns = result.get("elapsed_ns")
    dirty_paths = result.get("dirty_paths")
    if not isinstance(elapsed_ns, int) or elapsed_ns < 0:
        raise ValueError("git status timing is invalid")
    if not isinstance(dirty_paths, int) or dirty_paths < 0:
        raise ValueError("git status dirty path count is invalid")
    return GitStatusResult(
        version_id=version_id,
        elapsed_seconds=elapsed_ns / 1_000_000_000,
        dirty_paths=dirty_paths,
    )


def _run_git_commit(
    volume: Volume,
    image: Image,
    args: argparse.Namespace,
    clone_path: str,
) -> GitCommitResult:
    result_path = f"git-commit-{clone_path}.json"
    log_path = f"git-commit-{clone_path}.log"
    sandbox = Sandbox(
        image=image,
        cpu=args.cpu,
        memory=args.memory,
        env={
            "GIT_AUTHOR_NAME": "Chalk Volume Benchmark",
            "GIT_AUTHOR_EMAIL": "volume-benchmark@chalk.ai",
            "GIT_COMMITTER_NAME": "Chalk Volume Benchmark",
            "GIT_COMMITTER_EMAIL": "volume-benchmark@chalk.ai",
        },
        volumes=[volume.mount(MOUNT_PATH)],
        entrypoint=[
            "/bin/sh",
            "-c",
            STATUS_ENTRYPOINT,
            f"git-commit-{clone_path}",
            GIT_COMMIT_SCRIPT_DEST,
            MOUNT_PATH,
            result_path,
            log_path,
        ],
    )
    run_error: RuntimeError | None = None
    try:
        run_sandbox_workload(
            sandbox,
            timeout_secs=args.timeout_secs,
            phase=f"git commit on {clone_path} clone",
            log=(volume, log_path),
        )
    except RuntimeError as error:
        run_error = error

    try:
        version_id, result = _read_json(volume, result_path)
    except TimeoutError:
        if run_error is not None:
            raise run_error
        raise
    elapsed_ns = result.get("elapsed_ns")
    exit_code = result.get("exit_code")
    commit_sha = result.get("commit_sha")
    if not isinstance(elapsed_ns, int) or elapsed_ns < 0:
        raise ValueError("git commit timing is invalid")
    if not isinstance(exit_code, int) or exit_code < 0:
        raise ValueError("git commit exit code is invalid")
    if exit_code == 0 and (
        not isinstance(commit_sha, str)
        or len(commit_sha) != 40
        or any(character not in "0123456789abcdef" for character in commit_sha)
    ):
        raise ValueError("git commit SHA is invalid")
    if exit_code != 0 and commit_sha != "":
        raise ValueError("failed git commit unexpectedly produced a SHA")
    return GitCommitResult(
        version_id=version_id,
        elapsed_seconds=elapsed_ns / 1_000_000_000,
        exit_code=exit_code,
        commit_sha=commit_sha or None,
    )


def _benchmark(
    name: str,
    values: list[float],
    common: dict[str, Any],
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "unit": "second", **common, **metadata},
        "runs": [{"values": values}],
    }


def build_pyperf_results(
    *,
    args: argparse.Namespace,
    repo_file_count: int,
    fuse_clone: FuseCloneResult,
    sdk_clone: SdkCloneResult,
    local_clone_seconds: float,
    statuses: dict[str, GitStatusResult],
    commits: dict[str, GitCommitResult],
) -> dict[str, Any]:
    common = {
        "workload": "git-repo",
        "repo_url": args.repo_url,
        "repo_ref": args.repo_ref,
        "repo_file_count": repo_file_count,
        "sandbox_cpu": args.cpu,
        "sandbox_memory": args.memory,
    }
    benchmarks = [
        _benchmark(
            "fuse-clone-write",
            [fuse_clone.write_seconds],
            common,
            clone_path="fuse",
            volume_version_id=fuse_clone.version_id,
        ),
        _benchmark(
            "fuse-clone-commit",
            [fuse_clone.commit_seconds],
            common,
            clone_path="fuse",
            volume_version_id=fuse_clone.version_id,
        ),
        _benchmark(
            "sdk-load-git-repo",
            [sdk_clone.elapsed_seconds],
            common,
            clone_path="sdk",
            volume_version_id=sdk_clone.version_id,
            volume_versions_created=sdk_clone.versions_created,
        ),
        _benchmark(
            "local-git-clone",
            [local_clone_seconds],
            common,
            clone_path="sdk",
        ),
    ]
    for clone_path in ("fuse", "sdk"):
        status = statuses[clone_path]
        benchmarks.append(
            _benchmark(
                f"git-status-on-{clone_path}-clone",
                [status.elapsed_seconds],
                common,
                clone_path=clone_path,
                git_command="status --porcelain=v1 --untracked-files=all",
                git_status_dirty_paths=status.dirty_paths,
                volume_version_id=status.version_id,
            )
        )
    for clone_path in ("fuse", "sdk"):
        commit = commits[clone_path]
        commit_metadata: dict[str, Any] = {
            "clone_path": clone_path,
            "git_command": 'commit -am "Benchmark mounted Git commit"',
            "git_exit_code": commit.exit_code,
            "volume_version_id": commit.version_id,
        }
        if commit.commit_sha is not None:
            commit_metadata["git_commit_sha"] = commit.commit_sha
        benchmarks.append(
            _benchmark(
                f"git-commit-on-{clone_path}-clone",
                [commit.elapsed_seconds],
                common,
                **commit_metadata,
            )
        )
    return {"version": "1.0", "metadata": common, "benchmarks": benchmarks}


def main() -> dict[str, Any]:
    args = parse_args()
    with ExitStack() as stack:
        volumes = {
            clone_path: stack.enter_context(
                temporary_volume(
                    f"sandbox-git-repo-{clone_path}", keep=args.keep_volumes
                )
            )
            for clone_path in ("fuse", "sdk")
        }
        image = _benchmark_image()
        fuse_clone = _run_fuse_clone(volumes["fuse"], image, args)
        sdk_clone = _run_sdk_clone(volumes["sdk"], args)
        local_clone_seconds, local_paths = _run_local_clone(args)

        if fuse_clone.repo_file_count != len(local_paths):
            raise ValueError(
                f"FUSE clone has {fuse_clone.repo_file_count} files, expected "
                + f"{len(local_paths)}"
            )

        statuses: dict[str, GitStatusResult] = {}
        for clone_path, volume in volumes.items():
            statuses[clone_path] = _run_git_status(volume, image, args, clone_path)

        commits: dict[str, GitCommitResult] = {}
        for clone_path, volume in volumes.items():
            commits[clone_path] = _run_git_commit(volume, image, args, clone_path)

        results = build_pyperf_results(
            args=args,
            repo_file_count=len(local_paths),
            fuse_clone=fuse_clone,
            sdk_clone=sdk_clone,
            local_clone_seconds=local_clone_seconds,
            statuses=statuses,
            commits=commits,
        )
        failed_commits = [
            clone_path
            for clone_path, result in commits.items()
            if result.exit_code != 0
        ]
        if failed_commits:
            raise BenchmarkFailure(
                f"git commit failed on {', '.join(failed_commits)} clone(s)",
                results,
            )
        return results


if __name__ == "__main__":
    run_benchmark("volume-git-repo", main)
