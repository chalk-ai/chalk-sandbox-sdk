# Git inside a sandbox

`sandbox.git` exposes `SandboxGit` methods that execute Git inside a running
sandbox. The default `Image.sandbox()` includes Git, CA certificates, and OpenSSH
and uses `/workspace` as its working directory. Custom images must include Git.

## Clone, edit, and commit

```python
from chalkcompute import SandboxClient

with SandboxClient.from_env() as client:
    sandbox = client.create()
    try:
        sandbox.git.clone("https://github.com/octocat/Hello-World.git", "repo")
        sandbox.git.create_branch("repo", "update-docs")
        sandbox.git.checkout_branch("repo", "update-docs")
        sandbox.exec(
            "python", "-c",
            "from pathlib import Path; Path('NOTES.md').write_text('Sandbox notes\\n')",
            workdir="/workspace/repo",
        )
        sandbox.git.add("repo", ["NOTES.md"])
        commit_id = sandbox.git.commit(
            "repo", "Add sandbox notes", "Example User", "user@example.com"
        )
        print(commit_id)
        print(sandbox.git.status("repo"))
    finally:
        sandbox.terminate()
```

Repository `path` arguments are absolute sandbox paths or paths relative to the
image working directory. For file operations, pass the repository root as `path`;
`files` is a nonempty list of Git pathspecs relative to that directory. These are
sandbox files, not files on the SDK caller's machine.

Every method accepts keyword-only `timeout_secs=None`. This leaves the sandbox
exec timeout unset. A supplied timeout applies separately to each Git subprocess,
including each step of clone-at-commit, commit, and remote operations.

## Clone options and authentication

Clones default to the remote's default branch and `depth=1`. Use `branch="main"`
to select a branch or tag, `depth=None` for full history, or `commit_id="<commit>"`
to fetch a specific commit and check it out with detached HEAD. The remote must
allow fetching that commit. The destination must be absent or empty.

`clone`, `fetch`, `pull`, and `push` accept `username` and `password` together.
For HTTPS, the password can be an access token accepted by the Git host:

```python
import os

credentials = {
    "username": os.environ["GIT_USERNAME"],
    "password": os.environ["GIT_TOKEN"],
}
sandbox.git.clone(
    "https://github.com/example/private-repo.git", "private-repo", **credentials
)
sandbox.git.fetch("private-repo", **credentials)
```

Credentials are scoped to each operation, are not saved in Git configuration,
and do not change the remote URL. Supply them again for subsequent operations.
TLS verification remains enabled. Without these arguments, Git uses existing
Git/SSH authentication, including managed outbound SSH. Interactive credential
prompts are disabled. `remote_add(fetch=True)` uses existing authentication and
does not accept `username` or `password`.

## Method reference

All methods below are called on `sandbox.git`. Optional arguments after `path`
are keyword-only unless shown as positional. Methods return `None` unless a
return value is listed. Each method's Python docstring documents every parameter,
errors, and an example.

| Method | Behavior and options |
| --- | --- |
| `clone(url, path, ...)` | Clone with `branch=None`, `commit_id=None`, `depth=1`; supports HTTPS credentials. |
| `init(path, ...)` | Initialize a repository; `bare=False`, `initial_branch=None` (Git's default). |
| `add(path, files)` | Stage pathspecs, including deletions; `["."]` stages all changes under `path`. |
| `commit(path, message, author, email, ...)` | Commit staged changes and return the commit ID. `allow_empty=False`. Explicit identity applies to author and committer for this command only. |
| `status(path)` | Return `GitStatus` without fetching upstream changes. |
| `branches(path)` | Return local branch names as `list[str]`, excluding remote-tracking branches. |
| `create_branch(path, name)` | Create a local branch at HEAD without switching to it. |
| `checkout_branch(path, branch)` | Switch branches; conflicting local changes cause failure. |
| `delete_branch(path, name, ...)` | Delete a merged local branch; `force=True` allows unmerged deletion. Cannot delete a checked-out branch. |
| `fetch(path, ...)` | Fetch `remote="origin"`, optionally selecting `branch`; supports HTTPS credentials. |
| `pull(path, ...)` | Fast-forward only. With `remote=None, branch=None`, use the configured upstream. Supplying only `branch` selects `origin`. Supports HTTPS credentials. |
| `push(path, ...)` | Push HEAD to `remote="origin"`, or select `branch`/refspec. `set_upstream=False`; supports HTTPS credentials. No force option is added. |
| `reset(path, ...)` | Default `mode="mixed", target="HEAD"` unstages changes while preserving working files. `files` limits the reset to index entries and requires mixed mode. |
| `restore(path, files, ...)` | Restore working files from the index by default. `staged=True` restores the index from HEAD; add `worktree=True` to restore both. `source` selects another revision. |
| `remote_add(path, name, url, ...)` | Add a remote; `overwrite=True` updates an existing fetch URL, retaining an explicit push URL. `fetch=True` fetches afterward. Both default to False. |
| `remote_get(path, name)` | Return a `GitRemote` with its first fetch and push URLs as resolved by Git. |
| `remotes(path)` | Return `list[GitRemote]` for all named remotes. |
| `set_config(path, key, value)` | Write repository-local configuration only. |
| `get_config(path, key)` | Return a required repository-local value as `str`; missing keys raise `GitError`. Does not consult global configuration. |
| `configure_user(path, name, email)` | Persist repository-local `user.name` and `user.email`. `commit` still requires explicit author and email arguments. |

## Inspect changes and upstream state

`GitStatus`, `GitFileStatus`, and `GitRemote` can be imported from `chalkcompute`.

```python
status = sandbox.git.status("repo")
print(status.current_branch, status.upstream, status.ahead, status.behind)
for file in status.file_status:
    print(file.name, file.index, file.working_tree, file.original_name)
```

`current_branch` is `None` and `detached` is `True` for detached HEAD.
`upstream` names the configured upstream branch, such as `origin/main`, or is
`None`. `branch_published` means an upstream is configured; it does not verify
that the branch exists on the server.
`ahead` and `behind` use locally known upstream history and default to zero when
unavailable. Fetch first when fresh upstream information is needed.

`file_status` contains changed and untracked paths, excluding ignored files.
An empty list means a clean working tree. Each entry has a `name`, an `index`
(staged) code, a `working_tree` code, and `original_name` for renames or copies
(otherwise `None`). Codes are `.` (unchanged), `M` (modified), `A` (added),
`D` (deleted), `R` (renamed), `C` (copied), `T` (type changed), `U` (unmerged),
and `?` (untracked). Names preserve spaces and newlines.

## Unstage, restore, and publish

```python
# Unstage one file, keeping its working-tree edits.
sandbox.git.reset("repo", files=["NOTES.md"])

# Alternatively, restore the index entry from HEAD.
sandbox.git.restore("repo", ["NOTES.md"], staged=True)

# Publish a committed branch to a remote where you have write access.
sandbox.git.push("repo", branch="update-docs", set_upstream=True)
```

`reset` supports `soft`, `mixed`, `hard`, `merge`, and `keep`. Soft resets move
HEAD while retaining the index and working files; mixed resets also reset the
index. Hard resets overwrite working files and can discard local changes.
Merge and keep modes preserve local changes where Git permits. With `files`,
reset changes only the selected index entries and does not move HEAD.

`restore` without `staged=True` overwrites working files and can discard edits.
Pulls fail when history has diverged instead of creating a merge commit.

A failed Git subprocess raises `GitError`. Invalid locally validated arguments
raise `ValueError`; errors from sandbox execution propagate unchanged. Multi-step
operations are not transactional: for example, a clone can succeed before the
subsequent fetch of `commit_id` fails.
