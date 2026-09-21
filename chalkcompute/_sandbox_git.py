"""Git operations executed inside a running sandbox."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from chalkcompute._git import GitError

if TYPE_CHECKING:
    from chalkcompute._sandbox import Sandbox


@dataclass(frozen=True)
class GitFileStatus:
    """Git porcelain status codes for a path (including untracked files).

    Attributes
    ----------
    name
        Path relative to the repository root, including spaces or newlines.
    index
        Status in the staging area: "." for unchanged, "M" for modified,
        "A" for added, "D" for deleted, "R" for renamed, "C" for copied,
        "T" for type changed, "U" for unmerged, or "?" for untracked.
    working_tree
        Working-tree status using the same codes as index.
    original_name
        Previous path for a rename or copy; otherwise None.
    """

    name: str
    index: str
    working_tree: str
    original_name: str | None = None


@dataclass(frozen=True)
class GitStatus:
    """Repository state, including upstream divergence and changed files.

    Attributes
    ----------
    current_branch
        Local branch name, or None for detached HEAD.
    file_status
        Changed and untracked paths. An empty list means a clean working tree;
        ignored files are omitted.
    ahead
        Commits ahead of the locally known upstream; zero if unavailable.
    behind
        Commits behind the locally known upstream; zero if unavailable.
    branch_published
        Whether Git reports a configured upstream. This does not check whether
        the branch currently exists on the remote server.
    """

    current_branch: str | None
    file_status: list[GitFileStatus] = field(default_factory=list)
    ahead: int = 0
    behind: int = 0
    branch_published: bool = False


@dataclass(frozen=True)
class GitRemote:
    """A named remote's fetch and push URLs.

    Attributes
    ----------
    name
        Remote name, such as "origin".
    fetch_url
        First fetch URL returned by Git, including configured URL rewrites.
    push_url
        First push URL returned by Git; may differ from fetch_url.
    """

    name: str
    fetch_url: str
    push_url: str


def _argument(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"{name} must be a nonempty string without NUL bytes")
    return value


def _operand(value: str, name: str) -> str:
    value = _argument(value, name)
    if value.startswith("-"):
        raise ValueError(f"{name} must not start with '-'")
    return value


def _files(files: list[str]) -> list[str]:
    if not isinstance(files, list) or not files:
        raise ValueError("files must be a nonempty list of paths")
    return [_argument(path, "file") for path in files]


class SandboxGit:
    """Access as ``sandbox.git``; requires Git in the sandbox image.

    Paths are relative to the image's working directory. Commands use argv,
    never shell interpolation. ``timeout_secs`` uses the sandbox exec timeout.
    Credentials are scoped to one command and never written into Git config.
    Existing SSH configuration (including managed SSH) is used when no HTTPS
    credentials are supplied.
    """

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def _run(
        self,
        *args: str,
        path: str | None = None,
        timeout_secs: int | None = None,
        username: str | None = None,
        password: str | None = None,
        env: dict[str, str] | None = None,
    ) -> bytes:
        command_env = {"GIT_TERMINAL_PROMPT": "0", **(env or {})}
        config: list[str] = []
        if (username is None) != (password is None):
            raise ValueError("username and password must be supplied together")
        if username is not None and password is not None:
            for name, value in (("username", username), ("password", password)):
                _argument(value, name)
                if "\n" in value or "\r" in value:
                    raise ValueError(f"{name} must not contain newlines")
            command_env.update(CHALK_GIT_USERNAME=username, CHALK_GIT_PASSWORD=password)
            # An empty helper clears inherited helpers. The helper only responds
            # to 'get', so Git's store/erase notifications cannot persist secrets.
            helper = '!f() { if [ "$1" = get ]; then printf "username=%s\\npassword=%s\\n" "$CHALK_GIT_USERNAME" "$CHALK_GIT_PASSWORD"; fi; }; f'
            config = ["-c", "credential.helper=", "-c", "credential.helper=" + helper]
        result = self._sandbox.exec(
            "git",
            *config,
            *(["-C", _argument(path, "path")] if path is not None else []),
            *args,
            env=command_env,
            timeout_secs=timeout_secs,
        )
        if result.exit_code != 0 or result.error or result.signal:
            message = (
                result.error
                or result.stderr_text
                or result.stdout_text
                or f"exit code {result.exit_code}, signal {result.signal}"
            )
            if password:
                message = message.replace(password, "***")
            raise GitError(f"Git operation failed: {message.strip()}")
        return result.stdout

    def clone(
        self,
        url: str,
        path: str,
        *,
        branch: str | None = None,
        commit_id: str | None = None,
        depth: int | None = 1,
        username: str | None = None,
        password: str | None = None,
        timeout_secs: int | None = None,
    ) -> None:
        """Clone the remote default branch with one commit of history by default.

        Parameters
        ----------
        url
            Repository URL (HTTPS or SSH) or a Git-accessible local path.
        path
            Destination directory inside the sandbox, absolute or relative to
            the image working directory. Must be absent or empty.
        branch
            Branch or tag to clone. None uses the remote default branch.
        commit_id
            Commit to fetch and check out as detached HEAD after cloning.
            The remote must allow fetching that commit.
        depth
            Positive history depth; defaults to 1. None requests full history.
        username
            HTTPS username. Supply together with password; credentials are scoped
            to this operation and are not saved in Git configuration.
        password
            HTTPS password or access token. Supply together with username.
            Omit both to use existing Git/SSH authentication.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> sandbox.git.clone("https://github.com/octocat/Hello-World.git", "repo", depth=None)

        Notes
        -----
        TLS certificate verification remains enabled. Cloning a specific commit
        runs clone, fetch, and checkout with a separate timeout for each command.
        """
        _argument(url, "url")
        _argument(path, "path")
        if commit_id is not None:
            _operand(commit_id, "commit_id")
        args = ["clone"]
        if commit_id is not None:
            args.append("--no-checkout")
        if branch is not None:
            args += ["--branch", _operand(branch, "branch")]
        if depth is not None:
            if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
                raise ValueError("depth must be a positive integer")
            args += ["--depth", str(depth)]
        self._run(
            *args,
            "--",
            url,
            path,
            username=username,
            password=password,
            timeout_secs=timeout_secs,
        )
        if commit_id is not None:
            self._run(
                "fetch",
                *(["--depth", str(depth)] if depth is not None else []),
                "origin",
                commit_id,
                path=path,
                username=username,
                password=password,
                timeout_secs=timeout_secs,
            )
            self._run(
                "checkout",
                "--detach",
                "FETCH_HEAD",
                "--",
                path=path,
                timeout_secs=timeout_secs,
            )

    def init(
        self,
        path: str,
        *,
        bare: bool = False,
        initial_branch: str | None = None,
        timeout_secs: int | None = None,
    ) -> None:
        """Initialize a repository; leave the initial branch to Git by default.

        Parameters
        ----------
        path
            Directory to initialize inside the sandbox, absolute or relative to
            the image working directory.
        bare
            Create a bare repository without a working tree.
        initial_branch
            Initial branch name. None uses the Git installation default.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> sandbox.git.init("repo", initial_branch="main")
        """
        args = ["init"]
        if bare:
            args.append("--bare")
        if initial_branch is not None:
            args += ["--initial-branch", _operand(initial_branch, "initial_branch")]
        self._run(*args, "--", _argument(path, "path"), timeout_secs=timeout_secs)

    def add(
        self, path: str, files: list[str], *, timeout_secs: int | None = None
    ) -> None:
        """Stage paths relative to the repository root.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        files
            Nonempty list of Git pathspecs relative to path. Use ["."] to stage
            all changes beneath that directory, including deletions.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> sandbox.git.add("repo", ["README.md"])
        """
        self._run("add", "--", *_files(files), path=path, timeout_secs=timeout_secs)

    def commit(
        self,
        path: str,
        message: str,
        author: str,
        email: str,
        *,
        allow_empty: bool = False,
        timeout_secs: int | None = None,
    ) -> str:
        """Commit staged changes and return the commit ID, without changing config.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        message
            Nonempty commit message.
        author
            Author and committer name for this commit.
        email
            Author and committer email for this commit.
        allow_empty
            Allow a commit with no staged changes. Defaults to False.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Returns
        -------
        str
            The new commit ID. Identity is set only for this command; repository
            configuration is unchanged.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> commit_id = sandbox.git.commit("repo", "Update docs", "Example User", "user@example.com")
        """
        env = {
            "GIT_AUTHOR_NAME": _argument(author, "author"),
            "GIT_AUTHOR_EMAIL": _argument(email, "email"),
            "GIT_COMMITTER_NAME": author,
            "GIT_COMMITTER_EMAIL": email,
        }
        self._run(
            "commit",
            *(["--allow-empty"] if allow_empty else []),
            "-m",
            _argument(message, "message"),
            path=path,
            env=env,
            timeout_secs=timeout_secs,
        )
        return (
            self._run("rev-parse", "HEAD", path=path, timeout_secs=timeout_secs)
            .decode()
            .strip()
        )

    def status(self, path: str, *, timeout_secs: int | None = None) -> GitStatus:
        """Inspect the branch, upstream divergence, and changed files.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Returns
        -------
        GitStatus
            Current branch, locally known upstream divergence, and changed paths.
            Does not fetch; upstream information may be stale.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.

        Examples
        --------
        >>> status = sandbox.git.status("repo")
        """
        records = (
            self._run(
                "status",
                "--porcelain=v2",
                "--branch",
                "-z",
                path=path,
                timeout_secs=timeout_secs,
            )
            .decode("utf-8", "surrogateescape")
            .split("\0")
        )
        branch = None
        ahead = behind = 0
        published = False
        files: list[GitFileStatus] = []
        records_iter = iter(records)
        for record in records_iter:
            if record.startswith("# branch.head "):
                value = record.removeprefix("# branch.head ")
                branch = None if value == "(detached)" else value
            elif record.startswith("# branch.upstream "):
                published = True
            elif record.startswith("# branch.ab "):
                a, b = record.removeprefix("# branch.ab ").split()
                ahead, behind = int(a), -int(b)
            elif record.startswith(("1 ", "2 ", "u ")):
                parts = record.split(" ", {"1": 8, "2": 9, "u": 10}[record[0]])
                original = next(records_iter) if record.startswith("2 ") else None
                files.append(
                    GitFileStatus(parts[-1], parts[1][0], parts[1][1], original)
                )
            elif record.startswith("? "):
                files.append(GitFileStatus(record[2:], "?", "?"))
        return GitStatus(branch, files, ahead, behind, published)

    def branches(self, path: str, *, timeout_secs: int | None = None) -> list[str]:
        """List local branch names.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Returns
        -------
        list[str]
            Local branch names; excludes remote-tracking branches.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.

        Examples
        --------
        >>> branches = sandbox.git.branches("repo")
        """
        return (
            self._run(
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads/",
                path=path,
                timeout_secs=timeout_secs,
            )
            .decode()
            .splitlines()
        )

    def create_branch(
        self, path: str, name: str, *, timeout_secs: int | None = None
    ) -> None:
        """Create a branch at HEAD without checking it out.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        name
            New local branch name. The branch starts at the current HEAD;
            the working tree stays on the current branch.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> sandbox.git.create_branch("repo", "feature")
        """
        self._run(
            "branch", "--", _operand(name, "name"), path=path, timeout_secs=timeout_secs
        )

    def checkout_branch(
        self, path: str, branch: str, *, timeout_secs: int | None = None
    ) -> None:
        """Switch to a branch, preserving uncommitted changes where possible.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        branch
            Branch to switch to. Git may infer a local tracking branch from a
            unique remote-tracking branch. Conflicting local changes cause failure.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> sandbox.git.checkout_branch("repo", "feature")
        """
        self._run(
            "switch",
            "--",
            _operand(branch, "branch"),
            path=path,
            timeout_secs=timeout_secs,
        )

    def delete_branch(
        self,
        path: str,
        name: str,
        *,
        force: bool = False,
        timeout_secs: int | None = None,
    ) -> None:
        """Delete a merged local branch; unmerged deletion requires ``force=True``.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        name
            Local branch to delete. Git rejects deletion of a checked-out branch.
        force
            Allow deletion of an unmerged branch. Defaults to False.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> sandbox.git.delete_branch("repo", "feature")
        """
        self._run(
            "branch",
            "-D" if force else "-d",
            "--",
            _operand(name, "name"),
            path=path,
            timeout_secs=timeout_secs,
        )

    def fetch(
        self,
        path: str,
        *,
        remote: str = "origin",
        branch: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout_secs: int | None = None,
    ) -> None:
        """Fetch a remote, optionally limiting it to a branch.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        remote
            Remote name or URL. Defaults to "origin".
        branch
            Branch or refspec to fetch. None uses the remote fetch configuration.
        username
            HTTPS username. Supply together with password; credentials are scoped
            to this operation and are not saved in Git configuration.
        password
            HTTPS password or access token. Supply together with username.
            Omit both to use existing Git/SSH authentication.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> sandbox.git.fetch("repo", branch="main")
        """
        self._run(
            "fetch",
            _operand(remote, "remote"),
            *([_operand(branch, "branch")] if branch is not None else []),
            path=path,
            username=username,
            password=password,
            timeout_secs=timeout_secs,
        )

    def pull(
        self,
        path: str,
        *,
        remote: str | None = None,
        branch: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout_secs: int | None = None,
    ) -> None:
        """Fast-forward from the configured upstream by default; never auto-merge.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        remote
            Remote name or URL. None uses the configured upstream remote, or
            "origin" when branch is supplied.
        branch
            Branch or refspec to pull. None leaves selection to Git configuration.
            Diverged history raises GitError instead of creating a merge commit.
        username
            HTTPS username. Supply together with password; credentials are scoped
            to this operation and are not saved in Git configuration.
        password
            HTTPS password or access token. Supply together with username.
            Omit both to use existing Git/SSH authentication.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> sandbox.git.pull("repo")
        """
        args = ["pull", "--ff-only"]
        if remote is not None or branch is not None:
            args.append(_operand(remote if remote is not None else "origin", "remote"))
        if branch is not None:
            args.append(_operand(branch, "branch"))
        self._run(
            *args,
            path=path,
            username=username,
            password=password,
            timeout_secs=timeout_secs,
        )

    def push(
        self,
        path: str,
        *,
        remote: str = "origin",
        branch: str | None = None,
        set_upstream: bool = False,
        username: str | None = None,
        password: str | None = None,
        timeout_secs: int | None = None,
    ) -> None:
        """Push HEAD to origin by default.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        remote
            Remote name or URL. Defaults to "origin".
        branch
            Branch or refspec to push. None pushes HEAD (the current branch).
        set_upstream
            Set tracking information for the pushed branch. Defaults to False.
        username
            HTTPS username. Supply together with password; credentials are scoped
            to this operation and are not saved in Git configuration.
        password
            HTTPS password or access token. Supply together with username.
            Omit both to use existing Git/SSH authentication.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> sandbox.git.push("repo", branch="feature", set_upstream=True)
        """
        self._run(
            "push",
            *(["--set-upstream"] if set_upstream else []),
            _operand(remote, "remote"),
            _operand(branch, "branch") if branch is not None else "HEAD",
            path=path,
            username=username,
            password=password,
            timeout_secs=timeout_secs,
        )

    def reset(
        self,
        path: str,
        *,
        mode: str = "mixed",
        target: str = "HEAD",
        files: list[str] | None = None,
        timeout_secs: int | None = None,
    ) -> None:
        """Reset to HEAD by default, preserving working files (mixed reset).

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        mode
            Reset mode: "soft" moves HEAD; "mixed" also resets the index;
            "hard" also overwrites working files and can discard local changes.
            "merge" and "keep" preserve local changes where Git permits.
            Defaults to "mixed"; files is supported only with this mode.
        target
            Commit or revision to reset to. Defaults to "HEAD".
        files
            Optional nonempty list of pathspecs relative to path. Resets only
            those index entries to target without moving HEAD or changing files.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> sandbox.git.reset("repo", files=["README.md"])
        """
        if mode not in ("soft", "mixed", "hard", "merge", "keep"):
            raise ValueError("mode must be soft, mixed, hard, merge, or keep")
        if files is not None and mode != "mixed":
            raise ValueError("files can only be used with mixed reset")
        args = [
            "reset",
            *([] if files is not None else ["--" + mode]),
            _operand(target, "target"),
            "--",
        ]
        if files is not None:
            args += _files(files)
        self._run(*args, path=path, timeout_secs=timeout_secs)

    def restore(
        self,
        path: str,
        files: list[str],
        *,
        staged: bool = False,
        worktree: bool = False,
        source: str | None = None,
        timeout_secs: int | None = None,
    ) -> None:
        """Restore working files by default, or the index with ``staged=True``.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        files
            Nonempty list of Git pathspecs relative to path to restore.
        staged
            Restore the index. Defaults to False.
        worktree
            Explicitly restore working files, optionally together with staged.
            When neither flag is set, Git restores working files. Restoring working
            files can discard local changes.
        source
            Revision to restore from. None uses HEAD when staged=True, or the
            index otherwise.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> sandbox.git.restore("repo", ["README.md"], staged=True)
        """
        args = ["restore"]
        if staged:
            args.append("--staged")
        if worktree:
            args.append("--worktree")
        if source is not None:
            args += ["--source", _operand(source, "source")]
        self._run(*args, "--", *_files(files), path=path, timeout_secs=timeout_secs)

    def remote_add(
        self,
        path: str,
        name: str,
        url: str,
        *,
        fetch: bool = False,
        overwrite: bool = False,
        timeout_secs: int | None = None,
    ) -> None:
        """Add a remote. Replacing its URL requires ``overwrite=True``.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        name
            Remote name to add or update.
        url
            Fetch URL for the remote.
        fetch
            Fetch from the remote after adding or updating it. Uses existing
            authentication; this method has no username/password arguments.
        overwrite
            Update the fetch URL if the remote already exists. Defaults to False;
            an existing remote then raises GitError. An explicit push URL is retained.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> sandbox.git.remote_add("repo", "origin", "git@github.com:example/repo.git")
        """
        _operand(name, "name")
        _operand(url, "url")
        names = (
            self._run("remote", path=path, timeout_secs=timeout_secs)
            .decode()
            .splitlines()
            if overwrite
            else []
        )
        self._run(
            "remote",
            "set-url" if name in names else "add",
            name,
            url,
            path=path,
            timeout_secs=timeout_secs,
        )
        if fetch:
            self.fetch(path, remote=name, timeout_secs=timeout_secs)

    def remote_get(
        self, path: str, name: str, *, timeout_secs: int | None = None
    ) -> GitRemote:
        """Get the fetch/push URL for a named remote.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        name
            Existing remote name.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Returns
        -------
        GitRemote
            Remote name and its first fetch and push URLs, as resolved by Git.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> remote = sandbox.git.remote_get("repo", "origin")
        """
        _operand(name, "name")
        fetch = (
            self._run("remote", "get-url", name, path=path, timeout_secs=timeout_secs)
            .decode()
            .strip()
        )
        push = (
            self._run(
                "remote",
                "get-url",
                "--push",
                name,
                path=path,
                timeout_secs=timeout_secs,
            )
            .decode()
            .strip()
        )
        return GitRemote(name, fetch, push)

    def remotes(self, path: str, *, timeout_secs: int | None = None) -> list[GitRemote]:
        """List named remotes and their fetch/push URLs.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Returns
        -------
        list[GitRemote]
            Named remotes with their first fetch and push URLs, as resolved by Git.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.

        Examples
        --------
        >>> remotes = sandbox.git.remotes("repo")
        """
        names = (
            self._run("remote", path=path, timeout_secs=timeout_secs)
            .decode()
            .splitlines()
        )
        return [
            self.remote_get(path, name, timeout_secs=timeout_secs) for name in names
        ]

    def set_config(
        self, path: str, key: str, value: str, *, timeout_secs: int | None = None
    ) -> None:
        """Set repository-local Git configuration (never global configuration).

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        key
            Repository-local configuration key, such as "user.name".
        value
            Configuration value to store. Global configuration is unchanged.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> sandbox.git.set_config("repo", "pull.ff", "only")
        """
        self._run(
            "config",
            "--local",
            "--",
            _operand(key, "key"),
            value,
            path=path,
            timeout_secs=timeout_secs,
        )

    def get_config(
        self, path: str, key: str, *, timeout_secs: int | None = None
    ) -> str:
        """Read a required repository-local config value; missing keys raise GitError.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        key
            Required repository-local configuration key. Missing keys raise
            GitError; global configuration is not consulted.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Returns
        -------
        str
            Configuration value with trailing newlines removed.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> name = sandbox.git.get_config("repo", "user.name")
        """
        return (
            self._run(
                "config",
                "--local",
                "--get",
                "--",
                _operand(key, "key"),
                path=path,
                timeout_secs=timeout_secs,
            )
            .decode()
            .rstrip("\n")
        )

    def configure_user(
        self, path: str, name: str, email: str, *, timeout_secs: int | None = None
    ) -> None:
        """Set author identity for this repository only.

        Parameters
        ----------
        path
            Repository directory inside the sandbox, absolute or relative to the
            image working directory. Use the repository root for file operations.
        name
            Author/committer name to persist as repository-local user.name.
        email
            Author/committer email to persist as repository-local user.email.
            The commit helper still requires explicit author and email arguments.
        timeout_secs
            Timeout in seconds for each Git subprocess, not the whole operation.
            None leaves the sandbox exec timeout unset.

        Raises
        ------
        GitError
            A Git subprocess fails. Sandbox execution errors propagate unchanged.
        ValueError
            An argument fails local validation before the relevant Git command.

        Examples
        --------
        >>> sandbox.git.configure_user("repo", "Example User", "user@example.com")
        """
        self.set_config(
            path, "user.name", _argument(name, "name"), timeout_secs=timeout_secs
        )
        self.set_config(
            path, "user.email", _argument(email, "email"), timeout_secs=timeout_secs
        )
