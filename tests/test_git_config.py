import pytest

from chalkcompute import GitConfig


def test_identity_sets_author_and_committer() -> None:
    assert dict(GitConfig(name="Ada Lovelace", email="ada@example.com")) == {
        "GIT_AUTHOR_NAME": "Ada Lovelace",
        "GIT_COMMITTER_NAME": "Ada Lovelace",
        "GIT_AUTHOR_EMAIL": "ada@example.com",
        "GIT_COMMITTER_EMAIL": "ada@example.com",
    }


def test_unpacks_into_env() -> None:
    env = {**GitConfig(email="ada@example.com"), "LOG_LEVEL": "debug"}
    assert env == {
        "GIT_AUTHOR_EMAIL": "ada@example.com",
        "GIT_COMMITTER_EMAIL": "ada@example.com",
        "LOG_LEVEL": "debug",
    }


def test_config_uses_git_config_count(tmp_path) -> None:
    import os
    import subprocess

    git_config = GitConfig(
        name="Ada", config={"pull.rebase": "true", "init.defaultBranch": "main"}
    )
    assert git_config["GIT_CONFIG_COUNT"] == "2"
    result = subprocess.run(
        ["git", "config", "--get", "init.defaultBranch"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), **git_config},
    )
    assert result.stdout == "main\n", result.stderr


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"name": ""},
        {"email": " "},
        {"name": "a\0"},
        {"config": {"nodot": "x"}},
    ],
)
def test_invalid_values(kwargs) -> None:
    with pytest.raises(ValueError):
        GitConfig(**kwargs)


def test_config_values_must_be_strings() -> None:
    with pytest.raises(TypeError):
        GitConfig(config={"pull.rebase": True})  # ty: ignore[invalid-argument-type]
