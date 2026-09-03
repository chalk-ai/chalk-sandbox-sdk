from __future__ import annotations

from pathlib import Path

from chalkcompute import _git


def test_unpack_remote_to_mount_uses_blobless_checkout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    copies: list[tuple[Path, Path]] = []

    def run(command: list[str], **_kwargs: object) -> None:
        commands.append(command)

    monkeypatch.setattr(_git.subprocess, "run", run)
    monkeypatch.setattr(
        _git.shutil,
        "copytree",
        lambda source, destination: copies.append((source, destination)),
    )

    _git.unpack_remote_to_mount(
        "https://github.com/chalk-ai/example.git",
        tmp_path / "mount",
        "a1b2c3d4",
    )

    assert commands[0][:7] == [
        "git",
        "clone",
        "--depth=1",
        "--filter=blob:none",
        "--no-checkout",
        "--no-tags",
        "--",
    ]
    assert commands[1][-3:] == ["origin", "--", "a1b2c3d4"]
    assert commands[2][-3:] == ["checkout", "--force", "FETCH_HEAD"]
    assert copies == [(Path(commands[0][-1]) / ".git", tmp_path / "mount" / ".git")]


def test_unpack_remote_to_mount_terminates_fetch_options(
    monkeypatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> None:
        commands.append(command)

    monkeypatch.setattr(_git.subprocess, "run", run)
    monkeypatch.setattr(_git.shutil, "copytree", lambda _source, _destination: None)

    _git.unpack_remote_to_mount(
        "https://github.com/chalk-ai/example.git",
        tmp_path / "mount",
        "--upload-pack=/tmp/example",
    )

    assert commands[1][-3:] == ["origin", "--", "--upload-pack=/tmp/example"]
