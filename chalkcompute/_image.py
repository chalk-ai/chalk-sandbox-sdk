"""
Declarative image builder for Chalk sandboxes.

Build container images using a fluent API:

    from chalkcompute import Image, Sandbox

    img = (
        Image.debian_slim()
        .pip_install(["requests", "pandas"])
        .run_commands("apt-get update && apt-get install -y git")
        .workdir("/home/user/app")
        .env({"PROJECT_ROOT": "/home/user/app"})
    )

    sandbox = Sandbox(image=img).run()

The image specification is serialized as a protobuf ImageSpec and transmitted
to the sandbox service, which builds and caches the image before starting the
sandbox container.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shlex
import tarfile
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from chalkcompute._volume import Volume, VolumeMount

from protobuf import Oneof

import chalkcompute._proto_compat  # noqa: F401
from chalkcompute._gen.chalk.sandbox.v1 import service_pb as pb
from chalkcompute._naming import validate_env

logger = logging.getLogger(__name__)


def _build_step(field: str, value: object) -> pb.BuildStep:
    return pb.BuildStep(
        step=Oneof(field, value)  # ty: ignore[invalid-argument-type]
    )


ARCHIVE_NAME = "_archive.tar.gz"
_ARCHIVE_CHUNK_PREFIX = "_archive.part."
# PutFile payload is base64-encoded, so keep raw chunks under ~6 MB
# to stay well within typical HTTP body limits after encoding (~8 MB).
_ARCHIVE_CHUNK_SIZE = 6 * 1024 * 1024
# Copy content is base64-embedded into the image spec and generated Dockerfile.
# A 32 KiB raw budget stays under Argo object-size pressure after encoding and
# leaves headroom under Kaniko's 65,535-byte Dockerfile line limit.
_COPY_FILE_SIZE_LIMIT_BYTES = 32 * 1024
_COPY_TOTAL_SIZE_LIMIT_BYTES = 32 * 1024


@dataclass(frozen=True)
class LazyLocalFile:
    """A reference to a local file mounted as a volume at sandbox start."""

    src: str
    """Absolute local file path."""

    dest: str
    """Absolute destination path inside the container."""

    mode: int | None = None
    """Optional file permission mode."""

    mount_root: str | None = None
    """Optional mount directory overriding the default parent-based grouping."""


class Image:
    """Declarative, composable container image builder.

    Uses a fluent (method-chaining) API. Each method returns a new ``Image``
    instance so that intermediate images can be shared and extended.

    Call :meth:`build` to capture a reusable image with pinned file contents.
    Built images work with every ``image=`` parameter and can be transferred
    between processes using :meth:`to_json` and :meth:`from_json`.

    Examples
    --------
    >>> from chalkcompute import Image
    >>> img = (
    ...     Image.debian_slim()
    ...     .pip_install(["requests", "pandas"])
    ...     .workdir("/home/user/app")
    ... )
    """

    def __init__(self, base_image: str) -> None:
        self._base_image = base_image
        self._steps: list[pb.BuildStep] = []
        self._entrypoint: list[str] = []
        self._cmd: list[str] = []
        self._workdir: str | None = None
        self._env: dict[str, str] = {}
        self._lazy_local_files: set[LazyLocalFile] = set()
        self._volume_mounts: tuple[VolumeMount, ...] = ()
        self._built = False

    @property
    def volumes(self) -> tuple[VolumeMount, ...]:
        """Return pinned volumes retained by this image.

        Returns
        -------
        tuple of VolumeMount
            Pinned volumes retained independently of worker cleanup.
        """
        return self._volume_mounts

    def build(
        self,
        *,
        volumes: Iterable[tuple[str | Volume, str] | Volume | VolumeMount]
        | None = None,
        name: str | None = None,
        chalk_client: Any | None = None,
        poll_interval: float = 0.5,
        timeout: float = 600.0,
    ) -> Image:
        """Build a reusable image and capture immutable versions of its files.

        Parameters
        ----------
        volumes
            Additional volume references, accepting the same forms as Sandbox.
        name
            Optional retained source volume name for local volume files.
            Defaults to a stable name derived from the build recipe and file
            layout, excluding local source paths and volume file contents.
            Pass a name to share source volumes across recipes or make cleanup
            discoverable. Requires local volume files to upload.
        chalk_client
            Optional client for authentication. Defaults to the environment.
        poll_interval
            Polling interval in seconds for each build and source upload.
        timeout
            Timeout in seconds for each build and source upload.

        Returns
        -------
        Image
            A new built image with pinned volumes, leaving this recipe unchanged.

        Raises
        ------
        ValueError
            If a name is supplied without local volume files, or mounts conflict.

        Notes
        -----
        With no additional volumes, building an already built image returns
        an equivalent copy without network calls. Authentication, polling,
        and timeout settings only apply when a build or volume lookup is needed.

        Backing volumes persist independently of workers. Keep them while
        built images remain in use. Pins do not enforce runtime read-only access.
        """
        from chalkcompute._image_build import build_image_snapshot

        if name is not None and not self._lazy_local_files:
            raise ValueError(
                "Image.build(name=...) requires local volume files to upload"
            )
        if self._built:
            volumes = list(volumes) if volumes is not None else []
            if not volumes:
                return self._from_built(self._base_image, self._volume_mounts)
        return build_image_snapshot(
            self,
            volumes=volumes,
            name=name,
            chalk_client=chalk_client,
            poll_interval=poll_interval,
            timeout=timeout,
        )

    @classmethod
    def _from_built(cls, uri: str, volumes: Iterable[VolumeMount]) -> Image:
        """Create a built image from an image URI and pinned volumes.

        Parameters
        ----------
        uri
            Image reference for the built image.
        volumes
            Pinned volumes retained by the image.

        Returns
        -------
        Image
            A built image carrying the supplied volume pins.
        """
        image = cls.base(uri)
        image._volume_mounts = tuple(volumes)
        image._built = True
        return image

    def to_json(self) -> str:
        """Serialize a built image without local paths or credentials.

        Returns
        -------
        str
            JSON containing the image URI and pinned volumes.

        Raises
        ------
        ValueError
            If this image has not been built.
        """
        if not self._built:
            raise ValueError("Call Image.build() before serializing an image")
        return json.dumps(
            {
                "schema_version": 1,
                "image_uri": self._base_image,
                "volumes": [
                    {
                        "name": mount.name,
                        "mount_path": mount.mount_path,
                        "version_id": mount.version_id,
                    }
                    for mount in self._volume_mounts
                ],
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> Image:
        """Load a built image without contacting any service.

        Parameters
        ----------
        value
            JSON produced by :meth:`to_json`.

        Returns
        -------
        Image
            The built image represented by the JSON value.

        Raises
        ------
        ValueError
            If the JSON value is invalid or does not describe a built image.
        """
        from chalkcompute._image_build import merge_mounts
        from chalkcompute._volume import VolumeMount

        try:
            data = json.loads(value)
            if not isinstance(data, dict):
                raise ValueError("Invalid built image spec")
            if type(data["schema_version"]) is not int or data["schema_version"] != 1:
                raise ValueError("Unsupported built image schema version")
            if not isinstance(data["image_uri"], str):
                raise ValueError("image_uri must be a string")
            if not isinstance(data["volumes"], list):
                raise ValueError("volumes must be a list")
            mounts = [VolumeMount(**mount) for mount in data["volumes"]]
            for mount in mounts:
                if (
                    not isinstance(mount.name, str)
                    or not mount.name
                    or not isinstance(mount.mount_path, str)
                    or type(mount.version_id) is not int
                    or mount.version_id < 0
                    or mount.ref_name is not None
                ):
                    raise ValueError("Invalid pinned image mount")
            return cls._from_built(data["image_uri"], merge_mounts(mounts))
        except (TypeError, KeyError) as exc:
            raise ValueError("Invalid built image spec") from exc

    # -- Constructors ----------------------------------------------------------

    @classmethod
    def base(cls, image: str) -> Image:
        """Create an image from an arbitrary base image reference.

        Parameters
        ----------
        image
            Full image reference (e.g. ``"python:3.14-slim-trixie"``,
            ``"ghcr.io/org/image:tag"``).

        Returns
        -------
        Image
            A new ``Image`` based on the given reference.

        Examples
        --------
        >>> from chalkcompute import Image
        >>> img = Image.base("python:3.14-slim-trixie")
        >>> img = Image.base("ghcr.io/my-org/my-image:latest")
        """
        if not image or not image.strip():
            raise ValueError("base image reference cannot be empty")
        return cls(image)

    @classmethod
    def debian_slim(cls, python_version: str = "3.14") -> Image:
        """Create an image based on ``python:<version>-slim-trixie``.

        Parameters
        ----------
        python_version
            Python version tag.

        Returns
        -------
        Image
            A new ``Image`` based on the selected Python slim image.

        Examples
        --------
        >>> from chalkcompute import Image
        >>> img = Image.debian_slim()
        >>> img = Image.debian_slim("3.14.5")
        """
        return cls(f"python:{python_version}-slim-trixie")

    @classmethod
    def sandbox(cls, python_version: str = "3.14") -> Image:
        """Default sandbox image: Python, Git, TLS certificates, and SSH.

        Uses Debian slim with ``/workspace`` as its working directory. Builds
        lazily through the normal image builder, and can be extended with the
        same methods as any other Image. A fresh spec is returned each time.
        """
        return (
            cls.debian_slim(python_version)
            .apt_install(["git", "ca-certificates", "openssh-client"])
            .workdir("/workspace")
        )

    @classmethod
    def from_dockerfile(cls, path: str) -> Image:
        """Create an image from an existing Dockerfile.

        The Dockerfile contents are transmitted as a ``dockerfile_commands``
        step so that additional builder methods can be chained on top.

        Parameters
        ----------
        path
            Path to the Dockerfile.

        Returns
        -------
        Image
            A new ``Image`` whose build steps mirror the Dockerfile.

        Examples
        --------
        >>> from chalkcompute import Image
        >>> img = Image.from_dockerfile("./Dockerfile")
        >>> img = (
        ...     Image.from_dockerfile("./Dockerfile")
        ...     .pip_install(["rich"])
        ... )
        """
        content = Path(path).read_text()
        # Extract FROM line as base image, rest as commands
        lines = content.strip().splitlines()
        base = "scratch"
        commands: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.upper().startswith("FROM "):
                base = stripped[5:].strip()
            elif stripped and not stripped.startswith("#"):
                commands.append(stripped)
        img = cls(base)
        if commands:
            img._steps.append(
                _build_step(
                    "dockerfile_commands",
                    pb.DockerfileCommandsStep(commands=commands),
                )
            )
        return img

    # -- Builder methods -------------------------------------------------------

    def _copy(self) -> Image:
        """Return a shallow copy so that chaining is immutable."""
        new = Image(self._base_image)
        new._steps = list(self._steps)
        new._entrypoint = list(self._entrypoint)
        new._cmd = list(self._cmd)
        new._workdir = self._workdir
        new._env = dict(self._env)
        new._lazy_local_files = set(self._lazy_local_files)
        new._volume_mounts = self._volume_mounts
        return new

    def run_commands(self, *commands: str) -> Image:
        """Run shell commands during the image build.

        Each string is executed as a separate ``RUN`` instruction.

        Parameters
        ----------
        *commands
            Shell commands to run.

        Returns
        -------
        Image
            A new ``Image`` with the run steps appended.

        Examples
        --------
        >>> from chalkcompute import Image
        >>> img = (
        ...     Image.debian_slim()
        ...     .run_commands(
        ...         "apt-get update",
        ...         "apt-get install -y curl git",
        ...     )
        ... )
        """
        new = self._copy()
        new._steps.append(
            _build_step("run_commands", pb.RunCommandsStep(commands=list(commands)))
        )
        return new

    def apt_install(self, packages: Sequence[str]) -> Image:
        """Install Debian/Ubuntu system packages during the image build.

        Like :meth:`pip_install`, accepts a sequence and returns a new image.
        Requires ``apt-get`` and a root build user. Updates package indexes,
        installs noninteractively without recommended packages, and removes
        indexes in one build layer. Package versions can be pinned with
        ``name=version``. Other distributions can use :meth:`run_commands`
        with their package manager.

        Examples
        --------
        >>> image = Image.sandbox().apt_install(["tmux"])
        >>> sandbox = Sandbox(image=image).run()
        >>> sandbox.exec("tmux", "new-session", "-d", "-s", "work")
        """
        if isinstance(packages, (str, bytes)) or not isinstance(packages, Sequence):
            raise TypeError(
                "packages must be a sequence of package names, such as ['tmux']"
            )
        if not packages:
            raise ValueError("packages must not be empty")
        for package in packages:
            if (
                not isinstance(package, str)
                or not package.strip()
                or package.startswith("-")
                or "\0" in package
            ):
                raise ValueError(
                    "each package must be a nonempty name, not an apt option"
                )
        return self.run_commands(
            "apt-get update && DEBIAN_FRONTEND=noninteractive "
            "apt-get install -y --no-install-recommends -- "
            + shlex.join(packages)
            + " && rm -rf /var/lib/apt/lists/*"
        )

    def with_tmux(self) -> Image:
        """Install tmux with :meth:`apt_install`.

        Examples
        --------
        >>> image = Image.sandbox().with_tmux()
        >>> sandbox = Sandbox(image=image).run()
        >>> sandbox.exec("tmux", "new-session", "-d", "-s", "work")
        """
        return self.apt_install(["tmux"])

    def with_gh(self, version: str | None = None) -> Image:
        """Install the GitHub CLI from GitHub's apt repository.

        Debian's ``gh`` package lags upstream, so this adds
        ``cli.github.com/packages`` and installs from it. Requires ``apt-get``
        and a root build user. Set ``GH_TOKEN`` at runtime to authenticate.

        Parameters
        ----------
        version
            Optional ``gh`` version, such as ``"2.62.0"``. Defaults to the
            latest release when the image is first built.

        Examples
        --------
        >>> image = Image.sandbox().with_gh()
        >>> sandbox = Sandbox(
        ...     image=image,
        ...     secrets=[Secret.from_chalk_env("GITHUB_PAT", alias="GH_TOKEN")],
        ... ).run()
        >>> sandbox.exec("gh", "pr", "list")
        """
        package = "gh" if version is None else f"gh={_validate_version(version)}"
        return self._apt_repo_install(
            key_url="https://cli.github.com/packages/githubcli-archive-keyring.gpg",
            keyring="/etc/apt/keyrings/githubcli-archive-keyring.gpg",
            repo="https://cli.github.com/packages stable main",
            list_name="github-cli",
            package=package,
        )

    def with_claude_code(
        self,
        version: str | None = None,
        *,
        trusted_dirs: Sequence[str] = (),
    ) -> Image:
        """Install Claude Code and skip its first-run prompts.

        Installs the native ``claude`` binary from Anthropic's signed apt
        repository, so any Debian or Ubuntu base works without Node.js.
        Requires ``apt-get`` and a root build user. Apt installs do not
        auto-update, so a pinned version stays pinned in running sandboxes.
        Also writes ``~/.claude.json`` for the build user so that interactive
        ``claude`` starts without the onboarding flow, which otherwise ignores
        a ``CLAUDE_CODE_OAUTH_TOKEN`` or ``ANTHROPIC_API_KEY`` supplied at
        runtime.

        Parameters
        ----------
        version
            Optional Claude Code version, such as ``"2.1.270"``. Defaults to
            the latest release when the image is first built. Pin a version
            for reproducible images. The Debian revision ``-1`` is assumed
            unless the version includes one.
        trusted_dirs
            Absolute directories to pre-accept in Claude Code's "trust the
            files in this folder" prompt. Use the directory ``claude`` is
            started from. Only trust repositories whose Claude settings and
            hooks you are willing to run.

        Examples
        --------
        >>> image = (
        ...     Image.sandbox()
        ...     .with_claude_code(version="2.1.270", trusted_dirs=["/workspace/app"])
        ...     .with_gh()
        ...     .with_tmux()
        ...     .workdir("/workspace/app")
        ... )
        """
        if isinstance(trusted_dirs, (str, bytes)) or not isinstance(
            trusted_dirs, Sequence
        ):
            raise TypeError(
                "trusted_dirs must be a sequence of paths, such as ['/workspace/app']"
            )
        for path in trusted_dirs:
            if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
                raise ValueError("each trusted dir must be an absolute path")
        package = "claude-code"
        if version is not None:
            version = _validate_version(version)
            package += "=" + (version if "-" in version else version + "-1")
        config = json.dumps(
            {
                "hasCompletedOnboarding": True,
                "projects": {
                    path: {"hasTrustDialogAccepted": True} for path in trusted_dirs
                },
            }
        )
        return self._apt_repo_install(
            key_url="https://downloads.claude.ai/keys/claude-code.asc",
            keyring="/etc/apt/keyrings/claude-code.asc",
            repo="https://downloads.claude.ai/claude-code/apt/latest latest main",
            list_name="claude-code",
            package=package,
        ).run_commands(
            "printf '%s\\n' " + shlex.quote(config) + ' > "$HOME/.claude.json"'
        )

    def _apt_repo_install(
        self, *, key_url: str, keyring: str, repo: str, list_name: str, package: str
    ) -> Image:
        """Add a signed third-party apt repository and install one package."""
        return self.run_commands(
            "apt-get update && DEBIAN_FRONTEND=noninteractive "
            "apt-get install -y --no-install-recommends ca-certificates curl"
            " && install -d -m 755 /etc/apt/keyrings"
            f" && curl -fsSL {key_url} -o {keyring}"
            f" && chmod go+r {keyring}"
            f' && echo "deb [signed-by={keyring}] {repo}"'
            f" > /etc/apt/sources.list.d/{list_name}.list"
            " && apt-get update && DEBIAN_FRONTEND=noninteractive "
            "apt-get install -y --no-install-recommends -- "
            + shlex.quote(package)
            + " && rm -rf /var/lib/apt/lists/*"
        )

    def uv_pip_install(self, packages: Sequence[str]) -> Image:
        """Install Python packages via ``uv pip install``.

        Uses the ``uv`` resolver instead of ``pip`` for significantly faster
        installs. Requires a base image that has ``uv`` available.

        Parameters
        ----------
        packages
            List of pip requirement specifiers
            (e.g. ``["requests", "pandas"]``).

        Returns
        -------
        Image
            A new ``Image`` with the uv pip install step appended.

        Examples
        --------
        >>> from chalkcompute import Image
        >>> img = (
        ...     Image.base("ghcr.io/astral-sh/uv:python3.14-trixie-slim")
        ...     .uv_pip_install(["requests", "polars"])
        ... )
        """
        new = self._copy()
        new._steps.append(
            _build_step("uv_pip_install", pb.UvPipInstallStep(packages=list(packages)))
        )
        return new

    def uv_pip_install_from_requirements(self, path: str) -> Image:
        """Install packages from a ``requirements.txt`` file via ``uv pip install``.

        The file is read locally and its contents are inlined into the image
        spec so that the file does not need to be accessible at build time.
        Uses the ``uv`` resolver for significantly faster installs than
        :meth:`pip_install_from_requirements`.  Requires a base image that
        has ``uv`` available.

        Parameters
        ----------
        path
            Path to ``requirements.txt``.

        Returns
        -------
        Image
            A new ``Image`` with the uv pip install step appended.

        Examples
        --------
        >>> from chalkcompute import Image
        >>> img = (
        ...     Image.base("ghcr.io/astral-sh/uv:python3.14-trixie-slim")
        ...     .uv_pip_install_from_requirements("./requirements.txt")
        ... )
        """
        packages = [
            line.strip()
            for line in Path(path).read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return self.uv_pip_install(packages)

    def pip_install(self, packages: Sequence[str]) -> Image:
        """Install Python packages via pip.

        Parameters
        ----------
        packages
            List of pip requirement specifiers
            (e.g. ``["requests", "pandas"]``).

        Returns
        -------
        Image
            A new ``Image`` with the pip install step appended.

        Examples
        --------
        >>> from chalkcompute import Image
        >>> img = (
        ...     Image.debian_slim()
        ...     .pip_install(["requests", "pandas"])
        ... )
        """
        new = self._copy()
        new._steps.append(
            _build_step("pip_install", pb.PipInstallStep(packages=list(packages)))
        )
        return new

    def pip_install_from_requirements(self, path: str) -> Image:
        """Install packages from a ``requirements.txt`` file.

        The file is read locally and its contents are inlined into the image
        spec so that the file does not need to be accessible at build time.

        Parameters
        ----------
        path
            Path to ``requirements.txt``.

        Returns
        -------
        Image
            A new ``Image`` with the pip install step appended.

        Examples
        --------
        >>> from chalkcompute import Image
        >>> img = (
        ...     Image.debian_slim()
        ...     .pip_install_from_requirements("./requirements.txt")
        ... )
        """
        packages = [
            line.strip()
            for line in Path(path).read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return self.pip_install(packages)

    def add_local_file(
        self,
        src: str,
        dest: str,
        *,
        mode: int | None = None,
        strategy: Literal["copy", "volume"] = "volume",
    ) -> Image:
        """Add a local file into the image.

        Each call is **additive** — the file is appended to the set of
        files (or build steps) already registered on the image, not
        replacing them — so ``add_local_file`` can be chained many times
        to bring in several files.

        Parameters
        ----------
        src
            Local file path.
        dest
            Absolute destination path in the image.
        mode
            Optional POSIX file permission mode. Written in octal with a
            ``0o`` prefix; if omitted, ``strategy="volume"`` preserves the
            source file's on-disk permissions and ``strategy="copy"`` uses
            the image's default permissions. Common values:

            - ``0o755`` (``rwxr-xr-x``) — executable scripts and binaries;
              owner can read/write/execute, everyone else read/execute.
            - ``0o644`` (``rw-r--r--``) — regular data files; owner can
              read/write, everyone else read-only.
            - ``0o600`` (``rw-------``) — private files (credentials,
              secrets, SSH keys); owner read/write, no access for others.
            - ``0o700`` (``rwx------``) — owner-only directories or
              private executables.
            - ``0o400`` (``r--------``) — immutable/read-only files the
              workload must not mutate.
        strategy
            ``"copy"`` inlines the file contents into the image spec and
            rejects files larger than 32 KiB or additions that would make
            the image's total copied content exceed 32 KiB.
            ``"volume"`` (default) defers the file as a lazy reference
            to be mounted at sandbox start.

        Returns
        -------
        Image
            A new ``Image`` with the file registered for inclusion.

        Raises
        ------
        ValueError
            If ``strategy="copy"`` exceeds the per-file or total copied-content
            limit.

        Examples
        --------
        >>> from chalkcompute import Image
        >>> img = (
        ...     Image.debian_slim()
        ...     .add_local_file(
        ...         "./config.yaml",
        ...         "/app/config.yaml",
        ...     )
        ...     .add_local_file(
        ...         "./entrypoint.sh",
        ...         "/app/entrypoint.sh",
        ...         mode=0o755,
        ...         strategy="copy",
        ...     )
        ... )
        """
        if strategy not in ("copy", "volume"):
            raise ValueError(
                f"invalid strategy {strategy!r}, must be 'copy' or 'volume'"
            )
        new = self._copy()
        if strategy == "volume":
            _validate_lazy_local_file_destination(dest)
            src_path = Path(src).resolve()
            if not src_path.exists():
                raise FileNotFoundError(
                    f"add_local_file: source file not found: {str(src_path)!r}"
                )
            new._lazy_local_files.add(
                LazyLocalFile(src=str(src_path), dest=dest, mode=mode)
            )
        else:
            src_path = Path(src)
            content = _read_copy_file(src_path)
            _validate_copy_total_size(
                current_size=_inline_copy_size(new._steps),
                added_size=len(content),
                path=src_path,
            )
            step = pb.AddFileStep(destination=dest, content=content)
            if mode is not None:
                step.mode = mode
            new._steps.append(_build_step("add_file", step))
        return new

    def add_local_dir(
        self,
        src: str,
        dest: str,
        *,
        strategy: Literal["copy", "volume"] = "volume",
        exclude: Sequence[str] | None = None,
        archive: bool = False,
    ) -> Image:
        """Add an entire local directory into the image.

        Each call is **additive** — the directory's files are appended to
        the set already registered on the image, so ``add_local_dir`` can
        be chained with other ``add_local_dir`` / ``add_local_file`` calls
        to merge sources from multiple locations.

        Automatically respects ``.gitignore`` and ``.chalkignore`` files found
        at any level within ``src`` (each scoped to its directory subtree,
        matching go-git's behavior).  The ``.git/`` directory is always
        excluded.  Negation patterns (``!``) are supported.

        Parameters
        ----------
        src
            Local directory path.
        dest
            Absolute destination directory in the image.
        strategy
            ``"copy"`` inlines all file contents into the image spec and
            rejects files larger than 32 KiB or additions that would make
            the image's total copied content exceed 32 KiB.
            ``"volume"`` (default) defers each file as a lazy reference
            to be mounted at sandbox start.
        exclude
            Additional glob patterns to skip (matched against relative
            paths). These are applied on top of patterns loaded from
            ignore files, e.g. ``["data/**", "*.csv"]``.
        archive
            When ``True`` (volume strategy only), tar the directory into
            a single file for upload instead of individual files.
            The caller is responsible for extracting at the destination.

        Returns
        -------
        Image
            A new ``Image`` with the directory registered for inclusion.

        Raises
        ------
        ValueError
            If ``strategy="copy"`` exceeds the per-file or total copied-content
            limit.

        Examples
        --------
        >>> from chalkcompute import Image
        >>> img = (
        ...     Image.debian_slim()
        ...     .add_local_dir(
        ...         "./src",
        ...         "/app/src",
        ...         exclude=["*.pyc", "__pycache__"],
        ...     )
        ... )
        """
        from chalkcompute._ignore import (
            _match_exclude,
            collect_ignore_patterns,
            is_excluded as _is_pattern_excluded,
        )

        new = self._copy()
        src_path = Path(src)
        ignore_patterns = collect_ignore_patterns(src_path)
        _extra_exclude = exclude or []

        def _is_excluded(rel_str: str) -> bool:
            if _is_pattern_excluded(rel_str, ignore_patterns):
                return True
            return any(_match_exclude(rel_str, pat) for pat in _extra_exclude)

        def _iter_files() -> list[Path]:
            result = []
            for file_path in sorted(src_path.rglob("*")):
                if not file_path.is_file():
                    continue
                rel = file_path.relative_to(src_path)
                rel_str = str(rel)
                # Always skip .chalkignore files (at any depth).
                if rel_str.endswith(".chalkignore"):
                    continue
                if _is_excluded(rel_str):
                    continue
                result.append(file_path)
            return result

        if archive and strategy == "volume":
            files = _iter_files()
            if files:
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                    for file_path in files:
                        rel = file_path.relative_to(src_path)
                        tar.add(str(file_path), arcname=str(rel))
                data = buf.getvalue()
                # Split into chunks to stay within PutFile size limits.
                for i in range(0, len(data), _ARCHIVE_CHUNK_SIZE):
                    chunk = data[i : i + _ARCHIVE_CHUNK_SIZE]
                    tmp = tempfile.NamedTemporaryFile(
                        suffix=".tar.gz.part", delete=False
                    )
                    tmp.write(chunk)
                    tmp.close()
                    part_name = f"{_ARCHIVE_CHUNK_PREFIX}{i // _ARCHIVE_CHUNK_SIZE:04d}"
                    part_dest = os.path.join(dest, part_name)
                    new._lazy_local_files.add(
                        LazyLocalFile(src=tmp.name, dest=part_dest, mount_root=dest)
                    )
            return new

        copy_size = _inline_copy_size(new._steps)
        for file_path in _iter_files():
            rel = file_path.relative_to(src_path)
            file_dest = os.path.join(dest, str(rel))
            if strategy == "volume":
                new._lazy_local_files.add(
                    LazyLocalFile(
                        src=str(file_path.resolve()), dest=file_dest, mount_root=dest
                    )
                )
            else:
                content = _read_copy_file(file_path)
                copy_size = _validate_copy_total_size(
                    current_size=copy_size,
                    added_size=len(content),
                    path=file_path,
                )
                new._steps.append(
                    _build_step(
                        "add_file",
                        pb.AddFileStep(destination=file_dest, content=content),
                    )
                )
        return new

    def _app_module_names(self) -> set[str]:
        """Top-level module names already claimed under ``/app``."""
        dests = [lazy_file.dest for lazy_file in self._lazy_local_files] + [
            step.step.value.destination
            for step in self._steps
            if step.step is not None and step.step.field == "add_file"
        ]
        return {
            dest[len("/app/") :].split("/")[0].removesuffix(".py")
            for dest in dests
            if dest.startswith("/app/")
        }

    def add_local_python_source(self, *modules: str) -> Image:
        """Add local Python modules or packages to ``/app`` in the image.
        Wraps add_local_dir and add_local_file with sanity checking.

        Raises ValueError if a name is dotted, cannot be found on the local
        Python path, or does not resolve to a pure-Python module or package.

        Examples
        --------
        >>> from chalkcompute import Image
        >>> img = Image.debian_slim().add_local_python_source("helpers", "mypkg")
        """
        import importlib.util

        new = self
        for name in modules:
            if "." in name or "/" in name:
                raise ValueError(
                    f"add_local_python_source: {name!r} must be a top-level module name"
                )
            try:
                spec = importlib.util.find_spec(name)
            except (ImportError, ValueError):
                spec = None
            if spec is None:
                raise ValueError(f"add_local_python_source: module {name!r} not found")
            if spec.submodule_search_locations:
                locations = list(spec.submodule_search_locations)
                if len(locations) != 1:
                    raise ValueError(
                        f"add_local_python_source: namespace package {name!r} is not supported"
                    )
                new = new.add_local_dir(
                    locations[0],
                    f"/app/{name}",
                    exclude=["**/__pycache__/**", "**/*.pyc"],
                )
            elif spec.origin and spec.origin.endswith(".py"):
                new = new.add_local_file(spec.origin, f"/app/{name}.py")
            else:
                raise ValueError(
                    f"add_local_python_source: {name!r} is not a pure-Python module"
                )
        return new

    def dockerfile_commands(self, commands: Sequence[str]) -> Image:
        """Add raw Dockerfile instructions.

        These are injected verbatim into the generated Dockerfile. Each call
        is **additive** — instructions are appended to any previous build
        steps (including prior ``dockerfile_commands`` calls), not replacing
        them — so you can split a Dockerfile across multiple calls and
        interleave with other builder methods.

        Parameters
        ----------
        commands
            List of Dockerfile instructions
            (e.g. ``["RUN echo hello", "EXPOSE 8080"]``).

        Returns
        -------
        Image
            A new ``Image`` with the Dockerfile commands appended.

        Examples
        --------
        >>> from chalkcompute import Image
        >>> img = (
        ...     Image.debian_slim()
        ...     .dockerfile_commands([
        ...         "USER root",
        ...         "EXPOSE 8080",
        ...     ])
        ... )
        """
        new = self._copy()
        new._steps.append(
            _build_step(
                "dockerfile_commands",
                pb.DockerfileCommandsStep(commands=list(commands)),
            )
        )
        return new

    def env(self, variables: dict[str, str]) -> Image:
        """Set environment variables in the image.

        Parameters
        ----------
        variables
            Mapping of variable names to literal strings. Pass Secret values in
            the runtime consumer's env instead (for example, Sandbox(env=...)).

        Returns
        -------
        Image
            A new ``Image`` with the environment variables applied.

        Examples
        --------
        >>> from chalkcompute import Image
        >>> img = (
        ...     Image.debian_slim()
        ...     .env({
        ...         "LOG_LEVEL": "DEBUG",
        ...         "PORT": "8080",
        ...     })
        ... )
        """
        validate_env(variables)
        new = self._copy()
        new._env.update(variables)
        return new

    def workdir(self, path: str) -> Image:
        """Set the working directory in the image.

        Parameters
        ----------
        path
            Absolute path for the working directory.

        Returns
        -------
        Image
            A new ``Image`` with the working directory set.

        Examples
        --------
        >>> from chalkcompute import Image
        >>> img = (
        ...     Image.debian_slim()
        ...     .workdir("/home/user/app")
        ... )
        """
        new = self._copy()
        new._workdir = path
        return new

    def entrypoint(self, args: Sequence[str]) -> Image:
        """Set the container entrypoint.

        The ``ENTRYPOINT`` is the executable that always runs when the
        container starts.  Unlike :meth:`cmd` — which provides *default
        arguments* that callers can override at run time — the entrypoint
        is fixed: callers can't swap it out by passing a different command
        to ``Container.run`` or ``kubectl exec``, only append to it.

        Together, ``ENTRYPOINT`` and ``CMD`` form the full command that
        runs in the container:

        - With only an entrypoint: ``ENTRYPOINT`` runs with no args.
        - With only a CMD: the first element of ``CMD`` is treated as the
          executable and the rest as its arguments.
        - With both: the full command is ``ENTRYPOINT + CMD``, where
          ``CMD`` supplies the default (overridable) arguments.

        Each call **replaces** any previously set entrypoint.

        Use exec-form (``["python", "-m", "my_app"]``) rather than
        shell-form (``"python -m my_app"``) — exec-form skips the shell
        wrapper and makes signals (SIGTERM, SIGINT) reach your process
        directly, which matters for graceful shutdown.

        Parameters
        ----------
        args
            Entrypoint as exec-form list (e.g. ``["/bin/bash"]``).

        Returns
        -------
        Image
            A new ``Image`` with the entrypoint set.

        Examples
        --------
        >>> from chalkcompute import Image
        >>> img = (
        ...     Image.debian_slim()
        ...     .entrypoint(["python", "-m", "my_app"])
        ... )
        """
        new = self._copy()
        new._entrypoint = list(args)
        return new

    def cmd(self, args: Sequence[str]) -> Image:
        """Set the container ``CMD``.

        The ``CMD`` is the default argument list passed to the container's
        entrypoint when the container starts.  Unlike :meth:`entrypoint` — which
        sets the executable that always runs — ``CMD`` defines the *default
        arguments* that can be overridden at run time (for example, by a
        ``Container.run(command=[...])`` call or a Kubernetes pod spec).

        If no ``entrypoint`` is set, the first element of ``CMD`` is treated as
        the executable.  If an ``entrypoint`` is set, ``CMD`` is appended to it.

        Parameters
        ----------
        args
            CMD as exec-form list (e.g. ``["/bin/bash", "-l"]``).

        Returns
        -------
        Image
            A new ``Image`` with the CMD set.

        Examples
        --------
        Run ``python -m my_app`` by default, but allow the caller to override
        the module at run time:

        >>> from chalkcompute import Image
        >>> img = (
        ...     Image.debian_slim()
        ...     .entrypoint(["python"])
        ...     .cmd(["-m", "my_app"])
        ... )
        """
        new = self._copy()
        new._cmd = list(args)
        return new

    @property
    def lazy_local_files(self) -> set[LazyLocalFile]:
        """Local files deferred for volume-mounting at sandbox start.

        Returns
        -------
        set[LazyLocalFile]
            Copy of the set of lazy local files tracked by this image.
        """
        return set(self._lazy_local_files)

    def validate_lazy_local_files_for_volume_mounts(self) -> None:
        """Validate lazy local file destinations for volume mounting.

        Volume-backed lazy files are grouped by parent directory and mounted at
        that directory. Root-level destinations would require mounting at ``/``,
        which is not supported by container runtimes.

        Raises
        ------
        ValueError
            If a lazy local file would require mounting at the root directory.
        """
        self.lazy_local_files_grouped_for_volume_mounts()

    def lazy_local_files_grouped_for_volume_mounts(
        self,
    ) -> dict[str, list[LazyLocalFile]]:
        """Return lazy local files grouped by mount directory.

        Files from ``add_local_dir`` carry a ``mount_root`` that keeps all
        files from one call in a single volume.  Files from ``add_local_file``
        (``mount_root is None``) fall back to grouping by parent directory.

        Returns
        -------
        dict[str, list[LazyLocalFile]]
            Mapping from mount directory to the files that should be placed there.
        """
        grouped: dict[str, list[LazyLocalFile]] = {}
        for lf in sorted(
            self._lazy_local_files,
            key=lambda x: (x.mount_root or str(Path(x.dest).parent), x.dest, x.src),
        ):
            dest_dir = lf.mount_root or str(Path(lf.dest).parent)
            _validate_lazy_local_file_destination(lf.dest, mount_root=lf.mount_root)
            grouped.setdefault(dest_dir, []).append(lf)
        return grouped

    # -- Serialization ---------------------------------------------------------

    def as_image_uri_if_possible(self) -> str | None:
        """Return the image URI when no custom build is needed.

        A bare ``Image.base(...)`` or ``Image.debian_slim()`` is equivalent to
        its base image string. Once the image carries build steps, lazy files,
        or image config, callers must keep sending the full image spec.
        """
        if (
            self._steps
            or self._entrypoint
            or self._cmd
            or self._workdir is not None
            or self._env
            or self._lazy_local_files
        ):
            return None
        return self._base_image

    def to_proto(self) -> pb.ImageSpec:
        """Serialize this image definition to a protobuf ``ImageSpec``.

        Local volume files remain separate, as with from_proto(). Built
        images carrying volume pins must use to_json() because ImageSpec
        cannot represent those mounts.

        Returns
        -------
        pb.ImageSpec
            The protobuf representation of this image.
        """
        if self._volume_mounts:
            raise ValueError(
                "ImageSpec cannot preserve built image volumes. Use Image.to_json() "
                "or pass the Image directly to a compute resource."
            )
        return self._to_build_proto()

    def _to_build_proto(self) -> pb.ImageSpec:
        """Serialize the build recipe without runtime volume mounts.

        Returns
        -------
        pb.ImageSpec
            The protobuf representation of the build recipe.
        """
        spec = pb.ImageSpec(
            base_image=self._base_image,
            steps=list(self._steps),
            entrypoint=self._entrypoint,
            cmd=self._cmd,
        )
        if self._workdir is not None:
            spec.workdir = self._workdir
        for k, v in self._env.items():
            spec.env[k] = v
        return spec

    @classmethod
    def from_proto(cls, spec: pb.ImageSpec) -> Image:
        """Reconstruct an ``Image`` from an ``ImageSpec`` (inverse of :meth:`to_proto`; volume files excluded)."""
        img = cls(spec.base_image)
        img._steps = list(spec.steps)
        img._entrypoint = list(spec.entrypoint)
        img._cmd = list(spec.cmd)
        img._workdir = spec.workdir if spec.has_field("workdir") else None
        img._env = dict(spec.env)
        return img

    @classmethod
    def from_proto_bytes(cls, data: bytes) -> Image:
        """Reconstruct an ``Image`` from a serialized ``ImageSpec`` byte string."""
        return cls.from_proto(pb.ImageSpec.from_binary(data))

    def __repr__(self) -> str:
        parts = [f"Image(base={self._base_image!r}"]
        if self._built:
            parts.append(", built=True")
        if self._volume_mounts:
            parts.append(f", volumes={len(self._volume_mounts)}")
        if self._steps:
            parts.append(f", steps={len(self._steps)}")
        if self._workdir:
            parts.append(f", workdir={self._workdir!r}")
        if self._env:
            parts.append(f", env={self._env!r}")
        parts.append(")")
        return "".join(parts)


def _validate_version(version: str) -> str:
    if (
        not isinstance(version, str)
        or not version
        or version.startswith("-")
        or any(c.isspace() or c == "\0" for c in version)
    ):
        raise ValueError(f"invalid version {version!r}")
    return version


def _validate_lazy_local_file_destination(
    dest: str, *, mount_root: str | None = None
) -> None:
    dest_dir = mount_root or str(Path(dest).parent)
    if dest_dir == "/":
        raise ValueError(
            "add_local_file(..., strategy='volume') cannot target root paths "
            f"like {dest!r}; mounting a volume at '/' is not supported. "
            "Use strategy='copy' or place files under a subdirectory "
            "(for example '/app/app.py')."
        )


def _read_copy_file(path: Path) -> bytes:
    """Read a file only when its actual content fits the copy-file budget."""
    with path.open("rb") as file:
        content = file.read(_COPY_FILE_SIZE_LIMIT_BYTES + 1)
    if len(content) > _COPY_FILE_SIZE_LIMIT_BYTES:
        raise ValueError(
            f"{path} is at least {len(content)} bytes, which exceeds the "
            f"{_format_size_limit(_COPY_FILE_SIZE_LIMIT_BYTES)} per-file limit "
            "for strategy='copy'. "
            "Use strategy='volume' for larger files."
        )
    return content


def _inline_copy_size(steps: Sequence[pb.BuildStep]) -> int:
    """Return the raw bytes already embedded by copy-based add-file steps."""
    return sum(
        len(step.step.value.content)
        for step in steps
        if step.step is not None and step.step.field == "add_file"
    )


def _validate_copy_total_size(*, current_size: int, added_size: int, path: Path) -> int:
    """Return the new copied-content size or reject an oversized image spec."""
    total_size = current_size + added_size
    if total_size > _COPY_TOTAL_SIZE_LIMIT_BYTES:
        raise ValueError(
            f"adding {path} would inline {total_size} bytes, which exceeds the "
            f"{_format_size_limit(_COPY_TOTAL_SIZE_LIMIT_BYTES)} total limit "
            "for strategy='copy'. "
            "Use strategy='volume' for larger files or directories."
        )
    return total_size


def _format_size_limit(size: int) -> str:
    """Format a byte limit using the largest exact binary unit."""
    if size % (1024 * 1024) == 0:
        return f"{size // (1024 * 1024)} MiB"
    if size % 1024 == 0:
        return f"{size // 1024} KiB"
    return f"{size} bytes"
