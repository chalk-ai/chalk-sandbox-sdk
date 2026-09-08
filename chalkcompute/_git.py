"""Small helpers for materializing Git working trees."""

from __future__ import annotations

import subprocess
import shutil
import tempfile
from pathlib import Path


class GitError(RuntimeError):
    """Raised when a Git operation fails."""


def unpack_remote_to_mount(
    remote_url: str,
    mount_dest_dir: str | Path,
    revision: str = "main",
) -> None:
    """Check out a remote Git revision into a directory with Git metadata.

    The checkout writes directly to ``mount_dest_dir``. Git metadata is copied
    into the destination after checkout.
    """
    if not isinstance(remote_url, str) or not remote_url.strip():
        raise ValueError("remote_url must be a non-empty string")
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("revision must be a non-empty string")

    destination = Path(mount_dest_dir)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chalkcompute-git-") as temp_dir:
        repository = Path(temp_dir) / "repository"
        try:
            # Keep these commands in step with payloads/git_repo/clone.sh.
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth=1",
                    "--filter=blob:none",
                    "--no-checkout",
                    "--no-tags",
                    "--",
                    remote_url.strip(),
                    str(repository),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "fetch",
                    "--depth=1",
                    "--no-tags",
                    "origin",
                    "--",
                    revision.strip(),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    f"--git-dir={repository / '.git'}",
                    f"--work-tree={destination}",
                    "checkout",
                    "--force",
                    "FETCH_HEAD",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            shutil.copytree(repository / ".git", destination / ".git")
        except FileNotFoundError as exc:
            raise GitError("git executable was not found") from exc
        except subprocess.CalledProcessError as exc:
            message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            raise GitError(f"git command failed: {message}") from exc
