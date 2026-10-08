"""Small helpers for materializing Git working trees."""

from __future__ import annotations

import subprocess
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path


class GitError(RuntimeError):
    """Raised when a Git operation fails."""


class GitConfig(Mapping[str, str]):
    """Git identity and settings expressed as environment variables.

    A ``GitConfig`` is a mapping, so it can be passed as ``env`` directly or
    unpacked alongside other variables. Environment variables apply to every
    repository in the sandbox, override any ``.gitconfig``, and leave images
    shareable between users.

    Parameters
    ----------
    name
        Author and committer name (``GIT_AUTHOR_NAME``/``GIT_COMMITTER_NAME``).
    email
        Author and committer email (``GIT_AUTHOR_EMAIL``/``GIT_COMMITTER_EMAIL``).
    config
        Additional Git settings, such as ``{"pull.rebase": "true"}``, passed
        with ``GIT_CONFIG_COUNT`` (Git 2.31+). Only one ``GitConfig`` with
        ``config`` can be unpacked into the same env.

    Examples
    --------
    >>> from chalkcompute import GitConfig, Sandbox
    >>> sandbox = Sandbox(
    ...     env={
    ...         **GitConfig(name="Ada Lovelace", email="ada@example.com"),
    ...         "LOG_LEVEL": "debug",
    ...     },
    ... ).run()
    """

    def __init__(
        self,
        name: str | None = None,
        email: str | None = None,
        *,
        config: Mapping[str, str] | None = None,
    ) -> None:
        env: dict[str, str] = {}
        for label, value, keys in (
            ("name", name, ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
            ("email", email, ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")),
        ):
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                raise ValueError(f"{label} must be a nonempty string")
            env.update(dict.fromkeys(keys, value))
        if config:
            if not isinstance(config, Mapping):
                raise TypeError("config must be a mapping of Git keys to values")
            for i, (key, value) in enumerate(config.items()):
                if not isinstance(key, str) or "." not in key or "\0" in key:
                    raise ValueError(
                        f"invalid Git config key {key!r}; use section.name"
                    )
                if not isinstance(value, str) or "\0" in value:
                    raise TypeError(f"Git config value for {key!r} must be a string")
                env[f"GIT_CONFIG_KEY_{i}"] = key
                env[f"GIT_CONFIG_VALUE_{i}"] = value
            env["GIT_CONFIG_COUNT"] = str(len(config))
        if not env:
            raise ValueError("GitConfig needs a name, an email, or config")
        self._env = env

    def __getitem__(self, key: str) -> str:
        return self._env[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._env)

    def __len__(self) -> int:
        return len(self._env)

    def __repr__(self) -> str:
        return f"GitConfig({self._env!r})"


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
