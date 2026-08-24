"""Open a pull request on GitHub from an explicit set of file changes.

Pairs with :meth:`Volume.from_github` and :meth:`VersionInfo.diff`: compute a
:class:`~chalkcompute._volume.Diff` between two volume versions, then push it
to GitHub as a pull request. This never re-diffs anything server-side — the
pushed commit is built directly from the ``Diff`` the caller already has.
"""

from __future__ import annotations

from dataclasses import dataclass

from connectrpc.errors import ConnectError as ConnectRpcError

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.server.v1 import github_app_connect, github_app_pb
from chalkcompute._volume import Diff, FileChange, _resolve_github_installation


class GithubError(Exception):
    """Raised when opening a pull request from Chalk fails."""


@dataclass(frozen=True)
class PullRequestResult:
    """The pull request opened by :func:`open_pull_request`."""

    html_url: str
    number: int
    head_branch: str


def open_pull_request(
    repo: str,
    base: str,
    patch: Diff,
    *,
    title: str,
    body: str | None = None,
    draft: bool = False,
    connect_client: ConnectClient | None = None,
    timeout: float = 300.0,
) -> PullRequestResult:
    """Open a pull request against ``repo`` from a volume :class:`Diff`.

    ``patch`` is typically ``new_version.diff(old_version)`` — the changeset
    between two versions of the same volume. Each changed file becomes one
    file in a single pushed commit on a new branch; this call never touches
    a local git checkout or the volume itself, it just pushes ``patch``'s
    changes straight to GitHub.

    Parameters
    ----------
    repo
        Repository to open the pull request against, as ``"owner/name"``.
    base
        Branch the pull request targets.
    patch
        The changeset to push, from :meth:`VersionInfo.diff`.
    title
        Pull request title.
    body
        Pull request body. Defaults to no description.
    draft
        Open the pull request as a draft.
    connect_client
        Optional :class:`ConnectClient` used to resolve the repository and
        authenticate the request. Defaults to one built from the ambient
        auth context.
    timeout
        RPC deadline, in seconds.

    Returns
    -------
    PullRequestResult
        The opened pull request's URL, number, and head branch.
    """
    if not patch:
        raise GithubError("patch has no changes; nothing to open a pull request for")

    connect = connect_client or ConnectClient()
    installation_id, owner, name = _resolve_github_installation(
        repo, connect=connect, timeout=timeout
    )

    stub = connect.stub(github_app_connect.GitHubAppServiceClientSync)
    try:
        response = connect.call(
            stub.create_pull_request_from_changes,
            github_app_pb.CreatePullRequestFromChangesRequest(
                installation_id=installation_id,
                owner=owner,
                repo=name,
                base_branch=base,
                title=title,
                body=body,
                draft=draft,
                changes=[_file_change_to_proto(change) for change in patch.changes],
            ),
            timeout=timeout,
        )
    except ConnectRpcError as exc:
        raise GithubError(
            f"failed to open pull request against {repo!r}: {exc}"
        ) from exc

    return PullRequestResult(
        html_url=response.html_url,
        number=response.number,
        head_branch=response.head_branch,
    )


def _file_change_to_proto(change: FileChange) -> github_app_pb.RepoFileChangeInput:
    if change.kind == "deleted":
        return github_app_pb.RepoFileChangeInput(path=change.path, delete=True)
    assert change.new_content is not None
    return github_app_pb.RepoFileChangeInput(
        path=change.path, content=change.new_content
    )
