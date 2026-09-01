from __future__ import annotations

import sys
import time

from chalkcompute import Sandbox, Volume, VolumeError

VOLUME_SPOOL_EXHAUSTED_EXIT_CODE = 75
VOLUME_SPOOL_EXHAUSTED_MESSAGE = "Chalk volume write spool exhausted"


class VolumeSpoolExhaustedError(RuntimeError):
    pass


def wait_for_completion(
    sandbox: Sandbox, timeout_secs: int, poll_interval: float = 1
) -> tuple[str, str | None]:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        info = sandbox.refresh()
        print(f"Sandbox {sandbox.id} status: {info.status}", flush=True)
        if info.status not in ("pending", "running"):
            return info.status, info.status_message
        time.sleep(poll_interval)
    raise TimeoutError(
        f"Sandbox {sandbox.id} did not complete within {timeout_secs} seconds"
    )


def print_volume_log(volume: Volume, path: str, timeout_secs: int = 15) -> None:
    deadline = time.monotonic() + timeout_secs
    while True:
        try:
            version_id = volume.latest_version().id
            data = volume.at_version(version_id).read_file(path)
            break
        except VolumeError as error:
            if time.monotonic() >= deadline:
                print(f"Unable to read sandbox log {path!r}: {error}", file=sys.stderr)
                return
            time.sleep(1)
    log = data.decode(errors="replace")
    print(log, end="" if log.endswith("\n") else "\n", file=sys.stderr, flush=True)


def run_sandbox(
    sandbox: Sandbox,
    timeout_secs: int,
    phase: str,
    *,
    log_volume: Volume | None = None,
    log_path: str | None = None,
) -> None:
    status, status_message = wait_for_completion(sandbox, timeout_secs)
    if status == "succeeded":
        return
    if log_volume is not None and log_path is not None:
        print_volume_log(log_volume, log_path)
    if status_message == f"Exit code {VOLUME_SPOOL_EXHAUSTED_EXIT_CODE}":
        raise VolumeSpoolExhaustedError(
            f"{phase} sandbox completed with status {status}: "
            f"{VOLUME_SPOOL_EXHAUSTED_MESSAGE}"
        )
    detail = f": {status_message}" if status_message else ""
    raise RuntimeError(f"{phase} sandbox completed with status {status}{detail}")
