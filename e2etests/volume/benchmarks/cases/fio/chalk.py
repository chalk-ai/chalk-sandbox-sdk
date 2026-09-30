"""Execute the shared FIO workload through the native SDK."""

from __future__ import annotations

from chalkcompute import Image, Sandbox, VolumeError
from .workload import (
    BASE_IMAGE,
    COMMIT_SCRIPT_DEST,
    FIO_SCRIPT_DEST,
    FIO_SCRIPT_SOURCE,
    FioConfig,
    FioResult,
    INSTALL_FIO,
    LOG_PATH,
    MOUNT_PATH,
    PAYLOADS,
    entrypoint,
    read_results,
)
from e2etests.volume.benchmarks.harness import (
    read_committed_volume,
    run_sandbox_workload,
    temporary_volume,
)


def run_chalk(
    config: FioConfig,
    *,
    volume_name: str | None = None,
    keep: bool = False,
    environment: str | None = None,
    region: str | None = None,
    cloud: str | None = None,
) -> FioResult:
    if any((environment, region, cloud)):
        raise ValueError("Configure placement through the native SDK environment")
    with temporary_volume("sandbox-volume-fio", name=volume_name, keep=keep) as volume:
        image = (
            Image.base(BASE_IMAGE)
            .run_commands(INSTALL_FIO)
            .add_local_file(
                str(FIO_SCRIPT_SOURCE), FIO_SCRIPT_DEST, mode=0o755, strategy="copy"
            )
            .add_local_file(
                str(PAYLOADS / "commit.sh"),
                COMMIT_SCRIPT_DEST,
                mode=0o755,
                strategy="copy",
            )
        )
        sandbox = Sandbox(
            image=image,
            cpu=config.cpu,
            memory=config.memory,
            env=config.environment(),
            volumes=[volume.mount(MOUNT_PATH)],
            entrypoint=entrypoint(),
        )
        run_sandbox_workload(
            sandbox,
            timeout_secs=config.timeout_secs,
            phase="fio",
            log=(volume, LOG_PATH),
        )
        committed = read_committed_volume(
            volume,
            lambda snapshot: read_results(snapshot.read_file),
            retry_errors=(VolumeError, ValueError),
            description="FIO results",
        )
        results, log, commit_seconds = committed.value
        return FioResult(
            results=results,
            log=log,
            metadata={
                "volume_provider": "chalk",
                "volume_version_id": committed.version_id,
                "volume_commit_seconds": commit_seconds,
                "volume_results_ready_seconds": committed.ready_seconds,
                "volume_read_attempts": committed.read_attempts,
                "persistence_operation": ".chalk-volume/commit",
            },
            timings={
                "volume-commit": commit_seconds,
                "volume-results-ready": committed.ready_seconds,
            },
        )
