"""Run Harbor task environments in Chalk sandboxes.

    harbor run -p <tasks> -a oracle -e chalk_harbor:ChalkSandboxEnvironment

Each Harbor environment (one per trial role) is one Chalk sandbox. The sandbox SDK is
synchronous, so every call is pushed onto a worker thread; Harbor runs trials
concurrently on one event loop and a blocking call would stall all of them.

Image resolution, in order:

1. ``[environment].docker_image`` in the task, used as-is.
2. ``environment/Dockerfile``, translated into a chalkcompute ``Image``: ``FROM`` becomes
   the base and every other instruction is replayed in order. ``COPY``/``ADD`` of
   build-context files are embedded as base64 tarballs inside ``RUN`` steps, because the
   image service receives instructions rather than a build context; embedding keeps the
   files visible to later ``RUN`` steps, which a volume-mounted ``add_local_dir`` would not.

Environment kwargs (``--ek key=value``):

* ``image_map`` -- ``src=dst[,src=dst...]``: rewrite a ``FROM`` or ``docker_image``
  reference, e.g. to a registry copy of a locally built base image.
* ``entrypoint`` -- JSON list run as the sandbox's main process instead of
  ``sleep infinity``; needed when the image's ENTRYPOINT starts a service the task uses.
* ``ready_command`` -- shell command polled after start until it exits 0, for entrypoint
  services the agent needs before its first step (the entrypoint runs asynchronously).
* ``volumes`` -- ``name:/mount/path[,name:/mount/path...]``: existing Chalk volumes to
  mount, for task data too large to bake into an image.
* ``lifetime`` -- max sandbox lifetime as a duration string (default ``3600s``), so a
  crashed harbor process does not leak sandboxes forever.
"""

from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import json
import re
import shlex
import tarfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from chalkcompute import Image, NetworkPolicy, Sandbox, SandboxClient
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.environments.definition import (
    effective_exec_cwd,
    parse_dockerfile_workdir,
    require_agent_environment_definition,
)
from harbor.environments.tar_transfer import (
    extract_dir_from_bytes,
    pack_dir_to_bytes,
    remote_pack_command,
    remote_unpack_command,
)
from harbor.models.task.config import EnvironmentConfig, NetworkMode
from harbor.models.trial.paths import TrialPaths

# Largest compressed context one COPY may embed. Build steps travel inside the image spec,
# so an unbounded payload would turn a stray COPY of a dataset into an opaque RPC failure.
_EMBED_BUDGET_BYTES = 4 * 1024 * 1024
# Runs a Harbor command string under bash when the image has it: agent install scripts
# use bash syntax, but minimal images (alpine, distroless-ish) only ship sh.
# The image service discards everything written under /workspace during a build (the
# sandbox runtime owns that path), so COPYs aimed there are staged here and restored into
# /workspace when the sandbox starts. RUN steps that write into /workspace are still lost.
WORKSPACE = "/workspace"
WORKSPACE_STAGING = "/opt/.harbor-workspace"
# The sandbox service rejects less memory than this per requested CPU.
_MIN_MEMORY_MB_PER_CPU = 2048
_SHELL_SHIM = 'if command -v bash >/dev/null 2>&1; then exec bash -c "$1"; else exec sh -c "$1"; fi'


class ChalkSandboxEnvironment(BaseEnvironment):
    def __init__(
        self,
        environment_dir: Path,
        environment_name: str,
        session_id: str,
        trial_paths: TrialPaths,
        task_env_config: EnvironmentConfig,
        *args: Any,
        image_map: str | None = None,
        entrypoint: str | list[str] | None = None,
        volumes: str | None = None,
        ready_command: str | None = None,
        lifetime: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            environment_dir=environment_dir,
            environment_name=environment_name,
            session_id=session_id,
            trial_paths=trial_paths,
            task_env_config=task_env_config,
            **kwargs,
        )
        self._image_map = _parse_image_map(image_map)
        # harbor JSON-decodes --ek values, so a list arrives already parsed.
        self._entrypoint = (
            json.loads(entrypoint) if isinstance(entrypoint, str) else entrypoint
        )
        self._volumes = _parse_volumes(volumes)
        self._ready_command = ready_command
        self._lifetime = lifetime or "3600s"
        self._workdir = parse_dockerfile_workdir(self._dockerfile_path)
        self._client: SandboxClient | None = None
        self._sandbox: Sandbox | None = None

    @staticmethod
    def type() -> str:
        return "chalk-sandbox"

    @classmethod
    def preflight(cls) -> None:
        # Resolving a client exercises the same credential lookup the trials will use
        # (CHALK_CLIENT_ID/SECRET, CHALK_WEB_IDENTITY_TOKEN_FILE, or `chalk login`).
        SandboxClient.from_env().close()

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        return EnvironmentResourceCapabilities(cpu_limit=True, memory_limit=True)

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        # The policy is fixed at sandbox creation, so no dynamic_network_policy.
        return EnvironmentCapabilities(
            disable_internet=True,
            network_allowlist=True,
            network_allowlist_hostnames=True,
            network_allowlist_ipv4_addresses=True,
            network_allowlist_ipv4_cidrs=True,
        )

    def _sandbox_network_policy(self) -> NetworkPolicy:
        """Map Harbor's network mode onto a Chalk egress policy.

        A sandbox with no routes has no egress at all (not even DNS), so "public" has to
        be granted explicitly as the all-IPv4 route.
        """
        policy = self.network_policy
        if policy.network_mode == NetworkMode.PUBLIC:
            return NetworkPolicy(allowed_routes=[NetworkPolicy.Route("0.0.0.0/0")])
        if policy.network_mode == NetworkMode.NO_NETWORK:
            return NetworkPolicy()
        routes, hosts = [], []
        for entry in policy.allowed_hosts:
            try:
                routes.append(
                    NetworkPolicy.Route(str(ipaddress.IPv4Network(entry, strict=False)))
                )
            except ValueError:
                hosts.append(entry)
        return NetworkPolicy(allowed_routes=routes, allowed_hosts=hosts)

    @property
    def _dockerfile_path(self) -> Path:
        return self.environment_dir / "Dockerfile"

    def _validate_definition(self) -> None:
        require_agent_environment_definition(
            self.environment_dir,
            docker_image=self.task_env_config.docker_image,
        )

    # -- lifecycle ---------------------------------------------------------

    async def start(self, force_build: bool) -> None:
        image = self._build_image()
        cpus = self._effective_cpus
        memory_mb = self._effective_memory_mb
        if cpus and memory_mb and memory_mb < cpus * _MIN_MEMORY_MB_PER_CPU:
            self.logger.debug(
                f"Raising memory from {memory_mb}Mi to the {cpus}-CPU minimum"
            )
            memory_mb = cpus * _MIN_MEMORY_MB_PER_CPU
        self._client = await asyncio.to_thread(SandboxClient.from_env)
        self._sandbox = await asyncio.to_thread(
            self._client.create,
            image,
            cpu=str(cpus) if cpus else None,
            memory=f"{memory_mb}Mi" if memory_mb else None,
            env=self._startup_env() or None,
            entrypoint=self._entrypoint,
            volumes=self._volumes or None,
            network_policy=self._sandbox_network_policy(),
            lifetime=self._lifetime,
            tags={"harbor.session": _label_value(self.session_id)},
        )
        self.logger.debug(
            f"Chalk sandbox {self._sandbox.id} started for {self.session_id}"
        )
        await self._restore_workspace()
        await self._wait_until_ready()
        await self.ensure_dirs(self._mount_targets(writable_only=True))
        await self._upload_environment_dir_after_start()

    async def _wait_until_ready(self, timeout_sec: float = 300.0) -> None:
        if not self._ready_command:
            return
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while True:
            result = await self.exec(self._ready_command, timeout_sec=30)
            if result.return_code == 0:
                return
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError(
                    f"ready_command did not succeed within {timeout_sec}s: {result.stderr or result.stdout}"
                )
            await asyncio.sleep(2)

    async def _restore_workspace(self) -> None:
        # `cp -n` keeps anything the entrypoint already restored or wrote; see WORKSPACE_STAGING.
        result = await self.exec(
            f"if [ -d {WORKSPACE_STAGING} ]; then mkdir -p {WORKSPACE} && cp -an {WORKSPACE_STAGING}/. {WORKSPACE}/; fi",
            timeout_sec=120,
        )
        if result.return_code != 0:
            raise RuntimeError(
                f"Failed to restore {WORKSPACE}: {result.stderr or result.stdout}"
            )

    async def stop(self, delete: bool) -> None:
        # Sandboxes are ephemeral; there is nothing to keep when delete=False.
        sandbox, self._sandbox = self._sandbox, None
        if sandbox is not None:
            try:
                await asyncio.to_thread(sandbox.terminate)
            except Exception as exc:  # noqa: BLE001 -- teardown must not mask the trial result
                self.logger.error(
                    f"Error terminating Chalk sandbox {sandbox.id}: {exc}"
                )
        client, self._client = self._client, None
        if client is not None:
            await asyncio.to_thread(client.close)

    # -- exec --------------------------------------------------------------

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        sandbox = self._require_sandbox()
        user = self._resolve_user(user)
        if user is not None and str(user) not in ("root", "0"):
            # The sandbox exec API has no user field; drop privileges inside the shell.
            command = (
                f"su -s /bin/sh {shlex.quote(str(user))} -c {shlex.quote(command)}"
            )
        result = await asyncio.to_thread(
            sandbox.exec,
            "sh",
            "-c",
            _SHELL_SHIM,
            "sh",
            command,
            timeout_secs=timeout_sec,
            workdir=effective_exec_cwd(
                cwd, self.task_env_config.workdir, self._workdir
            ),
            env=self._merge_env(env),
        )
        stderr = result.stderr.decode(errors="replace")
        if result.error:
            stderr = f"{stderr}\n{result.error}".strip()
        return ExecResult(
            stdout=result.stdout.decode(errors="replace"),
            stderr=stderr,
            # A signal-killed process (e.g. timeout) has no exit code; report it like a shell would.
            return_code=result.exit_code
            if result.exit_code is not None
            else 128 + (result.signal or 1),
        )

    # -- file transfer -----------------------------------------------------

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        sandbox = self._require_sandbox()
        parent = str(PurePosixPath(target_path).parent)
        await self.exec(f"mkdir -p {shlex.quote(parent)}", timeout_sec=30)
        await asyncio.to_thread(
            sandbox.fs.write_bytes, target_path, Path(source_path).read_bytes()
        )

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        sandbox = self._require_sandbox()
        remote_tar = f"/tmp/.hb-upload-{uuid.uuid4().hex[:8]}.tar.gz"
        buffer = await asyncio.to_thread(pack_dir_to_bytes, source_dir, compress=True)
        await asyncio.to_thread(sandbox.fs.write_bytes, remote_tar, buffer.getvalue())
        result = await self.exec(
            f"{remote_unpack_command(remote_tar, target_dir)} && rm -f {shlex.quote(remote_tar)}",
            timeout_sec=300,
        )
        if result.return_code != 0:
            raise RuntimeError(
                f"Failed to extract upload into {target_dir!r}: {result.stderr or result.stdout}"
            )

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        sandbox = self._require_sandbox()
        data = await asyncio.to_thread(sandbox.fs.read_bytes, source_path)
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        sandbox = self._require_sandbox()
        remote_tar = f"/tmp/.hb-download-{uuid.uuid4().hex[:8]}.tar.gz"
        result = await self.exec(
            remote_pack_command(source_dir, remote_tar), timeout_sec=300
        )
        if result.return_code != 0:
            raise RuntimeError(
                f"Failed to archive {source_dir!r}: {result.stderr or result.stdout}"
            )
        data = await asyncio.to_thread(sandbox.fs.read_bytes, remote_tar)
        await asyncio.to_thread(extract_dir_from_bytes, data, target_dir)
        await self.exec(f"rm -f {shlex.quote(remote_tar)}", timeout_sec=30)

    # -- image -------------------------------------------------------------

    def _build_image(self) -> str | Image:
        if self.task_env_config.docker_image:
            return self._image_map.get(
                self.task_env_config.docker_image, self.task_env_config.docker_image
            )
        return _image_from_dockerfile(self._dockerfile_path, self._image_map)

    def _require_sandbox(self) -> Sandbox:
        if self._sandbox is None:
            raise RuntimeError("Chalk sandbox not started. Call start() first.")
        return self._sandbox


def _parse_image_map(spec: str | None) -> dict[str, str]:
    if not spec:
        return {}
    pairs = (item.split("=", 1) for item in spec.split(",") if item.strip())
    return {src.strip(): dst.strip() for src, dst in pairs}


def _parse_volumes(spec: str | None) -> list[tuple[str, str]]:
    if not spec:
        return []
    pairs = (item.split(":", 1) for item in spec.split(",") if item.strip())
    return [(name.strip(), path.strip()) for name, path in pairs]


def _label_value(value: str) -> str:
    # Kubernetes label values: <=63 chars of [A-Za-z0-9._-], alphanumeric at both ends.
    return re.sub(r"[^A-Za-z0-9._-]", "-", value)[:63].strip("-._") or "harbor"


def _image_from_dockerfile(dockerfile: Path, image_map: dict[str, str]) -> Image:
    """Translate a single-stage Dockerfile into a chalkcompute Image, instruction by instruction."""
    from dockerfile_parse import DockerfileParser

    context = dockerfile.parent
    instructions = DockerfileParser(path=str(dockerfile)).structure
    stages = [i for i in instructions if i["instruction"] == "FROM"]
    if len(stages) != 1:
        raise ValueError(
            f"{dockerfile}: only single-stage Dockerfiles are supported, found {len(stages)} FROM"
        )

    image: Image | None = None
    commands: list[str] = []
    for instruction in instructions:
        name, value = instruction["instruction"], instruction["value"]
        if name in ("COMMENT", "HEALTHCHECK"):
            # Harbor runs task healthchecks itself through exec.
            continue
        if name == "FROM":
            base = re.split(r"\s+as\s+", value.strip(), flags=re.IGNORECASE)[0]
            image = Image.base(image_map.get(base, base))
        elif name in ("COPY", "ADD") and "--from" not in value:
            commands.extend(_embedded_copy(context, value, dockerfile))
        else:
            commands.append(instruction["content"].strip())
    assert image is not None
    return image.dockerfile_commands(commands) if commands else image


def _embedded_copy(context: Path, value: str, dockerfile: Path) -> list[str]:
    """Render ``COPY <src>... <dest>`` as RUN steps that unpack an embedded tarball.

    Follows Docker's rules: a directory source copies its contents into dest, a file
    source lands at dest, or inside it when dest ends in ``/`` or there are several sources.
    """
    args = [a for a in shlex.split(value) if not a.startswith("--")]
    *sources, dest = args
    if dest == WORKSPACE or dest.startswith(f"{WORKSPACE}/"):
        dest = WORKSPACE_STAGING + dest[len(WORKSPACE) :]
    into_dir = dest.endswith("/") or len(sources) > 1
    steps: list[str] = []
    for source in sources:
        src = (context / source).resolve()
        if not src.is_relative_to(context.resolve()):
            raise ValueError(
                f"{dockerfile}: COPY source {source!r} escapes the build context"
            )
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            if src.is_dir():
                tar.add(src, arcname=".")
                target_dir, rename = dest, None
            else:
                tar.add(src, arcname=src.name)
                if into_dir:
                    target_dir, rename = dest, None
                else:
                    target_dir, rename = (
                        str(PurePosixPath(dest).parent),
                        PurePosixPath(dest).name,
                    )
        payload = buffer.getvalue()
        if len(payload) > _EMBED_BUDGET_BYTES:
            raise ValueError(
                f"{dockerfile}: COPY {source!r} is {len(payload)} bytes compressed, over the "
                f"{_EMBED_BUDGET_BYTES}-byte embed budget; mount it with --ek volumes=... instead"
            )
        encoded = base64.b64encode(payload).decode()
        unpack_dir = shlex.quote(target_dir)
        script = f"mkdir -p {unpack_dir} && echo {encoded} | base64 -d | tar -xz -C {unpack_dir}"
        if rename is not None and rename != src.name:
            script += f" && mv {shlex.quote(f'{target_dir}/{src.name}')} {shlex.quote(f'{target_dir}/{rename}')}"
        steps.append(f"RUN {script}")
    return steps


__all__ = ["ChalkSandboxEnvironment"]
