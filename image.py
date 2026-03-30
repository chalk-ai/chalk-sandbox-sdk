"""
Declarative image builder for Chalk sandboxes.

Build container images using a fluent API:

    from image import Image

    img = (
        Image.debian_slim("3.12")
        .pip_install(["requests", "pandas"])
        .run_commands("apt-get update && apt-get install -y git")
        .workdir("/home/user/app")
        .env({"PROJECT_ROOT": "/home/user/app"})
    )

    sandbox = client.create(image=img, name="my-sandbox")

The image specification is serialized as a protobuf ImageSpec and transmitted
to the sandbox service, which builds and caches the image before starting the
sandbox container.
"""

from __future__ import annotations

import os
from pathlib import Path

from chalk.sandbox.v1 import service_pb2 as pb


class Image:
    """Declarative, composable container image builder.

    Uses a fluent (method-chaining) API. Each method returns a new Image
    instance so that intermediate images can be shared and extended.
    """

    def __init__(self, base_image: str) -> None:
        self._base_image = base_image
        self._steps: list[pb.BuildStep] = []
        self._entrypoint: list[str] = []
        self._cmd: list[str] = []
        self._workdir: str | None = None
        self._env: dict[str, str] = {}

    # -- Constructors ----------------------------------------------------------

    @classmethod
    def base(cls, image: str) -> Image:
        """Create an image from an arbitrary base image reference.

        Args:
            image: Full image reference (e.g. "python:3.12-slim-bookworm",
                   "ghcr.io/org/image:tag").
        """
        return cls(image)

    @classmethod
    def debian_slim(cls, python_version: str = "3.12") -> Image:
        """Create an image based on python:<version>-slim-bookworm.

        Args:
            python_version: Python version tag (default "3.12").
        """
        return cls(f"python:{python_version}-slim-bookworm")

    @classmethod
    def from_dockerfile(cls, path: str) -> Image:
        """Create an image from an existing Dockerfile.

        The Dockerfile contents are transmitted as a dockerfile_commands step
        so that additional builder methods can be chained on top.

        Args:
            path: Path to the Dockerfile.
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
                pb.BuildStep(
                    dockerfile_commands=pb.DockerfileCommandsStep(commands=commands)
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
        return new

    def run_commands(self, *commands: str) -> Image:
        """Run shell commands during the image build.

        Each string is executed as a separate RUN instruction.

        Args:
            *commands: Shell commands to run.
        """
        new = self._copy()
        new._steps.append(
            pb.BuildStep(
                run_commands=pb.RunCommandsStep(commands=list(commands))
            )
        )
        return new

    def pip_install(self, packages: list[str]) -> Image:
        """Install Python packages via pip.

        Args:
            packages: List of pip requirement specifiers
                      (e.g. ["requests>=2.28", "pandas"]).
        """
        new = self._copy()
        new._steps.append(
            pb.BuildStep(
                pip_install=pb.PipInstallStep(packages=packages)
            )
        )
        return new

    def pip_install_from_requirements(self, path: str) -> Image:
        """Install packages from a requirements.txt file.

        The file is read locally and its contents are inlined into the image
        spec so that the file does not need to be accessible at build time.

        Args:
            path: Path to requirements.txt.
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
    ) -> Image:
        """Add a local file into the image.

        The file is read and its contents are inlined into the image spec.

        Args:
            src: Local file path.
            dest: Absolute destination path in the image.
            mode: Optional file permission mode (e.g. 0o755).
        """
        content = Path(src).read_bytes()
        step = pb.AddFileStep(destination=dest, content=content)
        if mode is not None:
            step.mode = mode
        new = self._copy()
        new._steps.append(pb.BuildStep(add_file=step))
        return new

    def add_local_dir(self, src: str, dest: str) -> Image:
        """Add an entire local directory into the image.

        All files are walked recursively and added individually.

        Args:
            src: Local directory path.
            dest: Absolute destination directory in the image.
        """
        new = self._copy()
        src_path = Path(src)
        for file_path in sorted(src_path.rglob("*")):
            if file_path.is_file():
                rel = file_path.relative_to(src_path)
                file_dest = os.path.join(dest, str(rel))
                content = file_path.read_bytes()
                new._steps.append(
                    pb.BuildStep(
                        add_file=pb.AddFileStep(destination=file_dest, content=content)
                    )
                )
        return new

    def dockerfile_commands(self, commands: list[str]) -> Image:
        """Add raw Dockerfile instructions.

        These are injected verbatim into the generated Dockerfile.

        Args:
            commands: List of Dockerfile instructions
                      (e.g. ["RUN echo hello", "EXPOSE 8080"]).
        """
        new = self._copy()
        new._steps.append(
            pb.BuildStep(
                dockerfile_commands=pb.DockerfileCommandsStep(commands=commands)
            )
        )
        return new

    def env(self, variables: dict[str, str]) -> Image:
        """Set environment variables in the image.

        Args:
            variables: Mapping of variable names to values.
        """
        new = self._copy()
        new._env.update(variables)
        return new

    def workdir(self, path: str) -> Image:
        """Set the working directory in the image.

        Args:
            path: Absolute path for the working directory.
        """
        new = self._copy()
        new._workdir = path
        return new

    def entrypoint(self, args: list[str]) -> Image:
        """Set the container entrypoint.

        Args:
            args: Entrypoint as exec-form list (e.g. ["/bin/bash"]).
        """
        new = self._copy()
        new._entrypoint = list(args)
        return new

    def cmd(self, args: list[str]) -> Image:
        """Set the container CMD.

        Args:
            args: CMD as exec-form list (e.g. ["/bin/bash", "-l"]).
        """
        new = self._copy()
        new._cmd = list(args)
        return new

    # -- Serialization ---------------------------------------------------------

    def to_proto(self) -> pb.ImageSpec:
        """Serialize this image definition to a protobuf ImageSpec."""
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

    def __repr__(self) -> str:
        parts = [f"Image(base={self._base_image!r}"]
        if self._steps:
            parts.append(f", steps={len(self._steps)}")
        if self._workdir:
            parts.append(f", workdir={self._workdir!r}")
        if self._env:
            parts.append(f", env={self._env!r}")
        parts.append(")")
        return "".join(parts)
