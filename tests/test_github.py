from __future__ import annotations

from typing import Any

import pytest

from chalkcompute import _github as gh
from chalkcompute._gen.chalk.server.v1 import github_app_pb
from chalkcompute._github import GithubError, PullRequestResult, open_pull_request
from chalkcompute._volume import Diff, FileChange
from tests._fakes import FakeConnectClient


def _patch_resolved_installation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    installation_id: str = "inst-1",
    owner: str = "chalk-ai",
    name: str = "customer-deployment",
) -> None:
    monkeypatch.setattr(
        gh, "_resolve_github_installation", lambda repo, *, connect, timeout: (installation_id, owner, name)
    )


def test_open_pull_request_rejects_empty_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    empty = Diff(volume_name="models", old_version_id=1, new_version_id=2, changes=())

    with pytest.raises(GithubError, match="no changes"):
        open_pull_request(
            "chalk-ai/customer-deployment",
            "main",
            empty,
            title="Add features",
            connect_client=FakeConnectClient(),  # type: ignore[arg-type]
        )


def test_open_pull_request_maps_changes_and_returns_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolved_installation(monkeypatch)
    patch = Diff(
        volume_name="models",
        old_version_id=1,
        new_version_id=2,
        changes=(
            FileChange(path="added.py", kind="added", new_content=b"new"),
            FileChange(
                path="changed.py", kind="modified", old_content=b"old", new_content=b"new2"
            ),
            FileChange(path="removed.py", kind="deleted", old_content=b"gone"),
        ),
    )
    requests: list[Any] = []

    def handler(method: str, request: Any) -> Any:
        if method == "create_pull_request_from_changes":
            requests.append(request)
            return github_app_pb.CreatePullRequestFromChangesResponse(
                html_url="https://github.com/chalk-ai/customer-deployment/pull/7",
                number=7,
                head_branch="chalk/pr-abc123",
            )
        raise AssertionError(method)

    connect = FakeConnectClient(handler=handler)

    result = open_pull_request(
        "chalk-ai/customer-deployment",
        "main",
        patch,
        title="Add features",
        body="see diff",
        draft=True,
        connect_client=connect,  # type: ignore[arg-type]
    )

    assert result == PullRequestResult(
        html_url="https://github.com/chalk-ai/customer-deployment/pull/7",
        number=7,
        head_branch="chalk/pr-abc123",
    )

    assert len(requests) == 1
    request = requests[0]
    assert request.installation_id == "inst-1"
    assert request.owner == "chalk-ai"
    assert request.repo == "customer-deployment"
    assert request.base_branch == "main"
    assert request.title == "Add features"
    assert request.body == "see diff"
    assert request.draft is True

    changes = {change.path: change for change in request.changes}
    assert changes["added.py"].content == b"new"
    assert changes["added.py"].delete is False
    assert changes["changed.py"].content == b"new2"
    assert changes["changed.py"].delete is False
    assert changes["removed.py"].delete is True
    assert changes["removed.py"].content == b""


def test_open_pull_request_wraps_rpc_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError as ConnectRpcError

    _patch_resolved_installation(monkeypatch)
    patch = Diff(
        volume_name="models",
        old_version_id=1,
        new_version_id=2,
        changes=(FileChange(path="a.py", kind="added", new_content=b"x"),),
    )

    def handler(method: str, request: Any) -> Any:
        raise ConnectRpcError(Code.INTERNAL, "boom")

    connect = FakeConnectClient(handler=handler)

    with pytest.raises(GithubError, match="failed to open pull request"):
        open_pull_request(
            "chalk-ai/customer-deployment",
            "main",
            patch,
            title="Add features",
            connect_client=connect,  # type: ignore[arg-type]
        )
