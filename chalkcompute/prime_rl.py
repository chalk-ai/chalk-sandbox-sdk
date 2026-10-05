"""Run prime-rl as a Chalk model training run.

    import chalkcompute

    run = chalkcompute.prime_rl.train("my_run.toml", gpu="nvidia-l4:2")
    run.wait()

or from a shell: ``python -m chalkcompute.prime_rl train my_run.toml``.

prime-rl itself is unmodified. The stock image runs ``uv run rl @ <config>``, and a forwarder
beside it reports each step's reward, loss, entropy, grad norm and learning rate to the run's
Metrics tab.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from chalkcompute._image import Image
    from chalkcompute._training import TrainingRunHandle

PRIME_RL_IMAGE = "primeintellect/prime-rl:main"
# The first chalkpy release whose metric allowlist includes the RL names.
CHALKPY = "chalkpy[runtime,training]>=2.159.0"
PLACEHOLDER_DATASET = "prime-rl-placeholder"

# Paths inside the prime-rl image: its checkout and venv, plus the venv the forwarder runs in.
_PRIME_RL_DIR = Path("/app")
_PRIME_RL_PYTHON = "/app/.venv/bin/python"
_METRICS_VENV = Path("/opt/chalk-metrics")
_OUTPUT_DIR = Path("/tmp/prime-rl-outputs")


def train(
    config: str,
    *,
    name: str = "prime-rl",
    gpu: str = "nvidia-l40s:2",
    cpu: str = "16",
    memory: str = "64Gi",
    max_steps: int | None = None,
    args: Iterable[str] = (),
    env: Mapping[str, str] | None = None,
    secrets: Iterable[str] = (),
    pip: Iterable[str] = (),
    env_packages: Iterable[str] = (),
    image: str = PRIME_RL_IMAGE,
    data: str | None = None,
) -> TrainingRunHandle:
    """Submit a prime-rl run and return its handle.

    ``config`` is a prime-rl TOML: a local file, uploaded with the run, or a path inside the
    image. ``args`` go to prime-rl's ``rl`` command. ``secrets`` are Chalk secret names exposed
    as environment variables of the same name. ``env_packages`` are local verifiers environment
    packages and ``pip`` extra requirement specs, both installed into prime-rl's venv.
    """
    from chalkcompute._dataset import DatasetClient
    from chalkcompute._secret import Secret
    from chalkcompute._training import TrainingError, TrainingFunction, training

    gpus = int(gpu.partition(":")[2] or 1)
    config_toml = Path(config).read_text() if Path(config).is_file() else None
    if data is None:
        # A training run must name a dataset; RL tasks come from prime-rl's environment instead.
        DatasetClient().upload(PLACEHOLDER_DATASET, {"placeholder": [0]})
        data = PLACEHOLDER_DATASET

    fn = training(
        _train_in_container,
        data=data,
        name=name,
        image=prime_rl_image(image, pip=pip, env_packages=env_packages),
        cpu=cpu,
        memory=memory,
        gpu=gpu,
        env={
            "HF_HOME": "/tmp/huggingface",
            **(env or {}),
            **{secret: Secret.from_chalk_env(secret) for secret in secrets},
        },
    )
    if not isinstance(fn, TrainingFunction):
        raise TrainingError(
            "prime_rl.train() submits a run and cannot be called from inside a training container"
        )
    run_args = [*(["--max-steps", str(max_steps)] if max_steps else []), *args]
    return fn.run_training(
        config={
            "gpus": gpus,
            "config_path": str(config),
            "config_toml": config_toml,
            "args": run_args,
        }
    )


def prime_rl_image(
    base: str = PRIME_RL_IMAGE,
    *,
    pip: Iterable[str] = (),
    env_packages: Iterable[str] = (),
) -> Image:
    """The prime-rl image plus chalkpy for the forwarder and any packages the run needs."""
    from chalkcompute._image import Image

    # chalkpy gets its own venv so it can't move the package versions vLLM and torch need.
    # The image runs as `appuser`, hence sudo.
    image = Image.base(base).run_commands(
        f"sudo python3.12 -m venv {_METRICS_VENV} && sudo {_METRICS_VENV}/bin/pip install -q '{CHALKPY}'"
    )
    # prime-rl's uv config rejects packages newer than its lock; --no-config installs into its venv anyway.
    install = f"uv pip install --no-config --python {_PRIME_RL_PYTHON}"
    for path in env_packages:
        # Under /tmp because build steps run as the image's user, who cannot write to /opt.
        dest = f"/tmp/prime-rl-env-packages/{Path(path).resolve().name}"
        image = image.add_local_dir(
            str(path),
            dest,
            strategy="copy",
            exclude=["**/__pycache__/**", "**/.git/**", "**/.venv/**"],
        ).run_commands(f"{install} {dest}")
    for spec in pip:
        image = image.run_commands(f"{install} '{spec}'")
    return image


def dashboard_url(run: TrainingRunHandle) -> str | None:
    """The run's page in the Chalk dashboard, or None if the environment lookup fails."""
    from chalkcompute._http import SYNC_HTTP_CLIENT

    client = run._client
    try:
        client._ensure_auth()
        api_server = client._api_server
        if not api_server:
            return None
        response = SYNC_HTTP_CLIENT.execute(
            "POST",
            f"{api_server}/chalk.server.v1.TeamService/GetEnv",
            headers={**(client._headers or {}), "Content-Type": "application/json"},
            content=b"{}",
            timeout=30,
        )
        if response.status != 200:
            return None
        environment = json.loads(response.content)["environment"]
    except Exception:  # noqa: BLE001 - the link is a convenience, the run is already submitted
        return None
    host = api_server.replace("://api.", "://", 1)
    return (
        f"{host}/projects/{environment['projectId']}/environments/{environment['id']}"
        f"/models/training-runs-v2/{run.run_id}"
    )


def _train_in_container(df: Any, config: dict[str, Any]) -> None:
    """The training function Chalk runs: prime-rl plus the metrics forwarder."""
    import resource
    import subprocess

    gpus = subprocess.run(
        ["nvidia-smi", "-L"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    print("\n".join(gpus), flush=True)
    if len(gpus) != config["gpus"]:
        raise RuntimeError(f"requested {config['gpus']} GPUs, found {len(gpus)}")
    # prime-rl's image entrypoint raises this limit, and the training entrypoint replaces it.
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = 32000 if hard == resource.RLIM_INFINITY else min(32000, hard)
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, target), hard))

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    config_path = Path(config["config_path"])
    if config.get("config_toml") is not None:
        config_path = _OUTPUT_DIR / "config.toml"
        config_path.write_text(config["config_toml"])
    rl_log = _OUTPUT_DIR / "rl.log"
    done = _OUTPUT_DIR / "done"
    # Run by path: the forwarder's venv has chalkpy, not chalkcompute.
    forwarder_script = Path(__file__).parent / "training" / "_prime_rl_metrics.py"
    forwarder = subprocess.Popen(
        [
            f"{_METRICS_VENV}/bin/python",
            str(forwarder_script),
            str(_OUTPUT_DIR),
            str(rl_log),
            str(done),
        ]
    )

    command = [
        "uv",
        "run",
        "rl",
        "@",
        str(config_path),
        "--output-dir",
        str(_OUTPUT_DIR),
        *config["args"],
    ]
    # The training entrypoint puts /app on PYTHONPATH; prime-rl's processes shouldn't import from it.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    with (
        rl_log.open("w") as log,
        subprocess.Popen(
            command,
            cwd=_PRIME_RL_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ) as rl,
    ):
        assert rl.stdout is not None
        for line in rl.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
            log.flush()

    done.touch()
    try:
        forwarder.wait(timeout=120)
    except subprocess.TimeoutExpired:
        forwarder.kill()
    if rl.returncode != 0:
        raise RuntimeError(f"prime-rl exited {rl.returncode}")


def main(argv: list[str] | None = None) -> int:
    """``python -m chalkcompute.prime_rl``: run prime-rl as a Chalk model training run."""
    parser = argparse.ArgumentParser(
        prog="python -m chalkcompute.prime_rl", description=main.__doc__
    )
    parser.add_argument(
        "--environment",
        help="Chalk environment id (default: the active one from `chalk login`)",
    )
    parser.add_argument(
        "--api-server", help="Chalk API server (default: the one from `chalk login`)"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser(
        "train",
        help="submit a run and follow it to completion",
        epilog="Arguments after -- go to prime-rl's `rl` command, e.g. `-- --orchestrator.batch-size 64`.",
    )
    run_parser.add_argument(
        "config",
        help="prime-rl TOML: a local file (uploaded with the run) or a path inside the image",
    )
    run_parser.add_argument("--name", default="prime-rl")
    run_parser.add_argument(
        "--gpu", default="nvidia-l40s:2", help="type:count; prime-rl needs at least 2"
    )
    run_parser.add_argument(
        "--cpu", default="16", help="250m, 500m, or a power-of-two core count"
    )
    run_parser.add_argument("--memory", default="64Gi")
    run_parser.add_argument("--max-steps", type=int)
    run_parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="environment variable for the run",
    )
    run_parser.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="NAME",
        help="Chalk secret exposed as an env var of the same name, e.g. HF_TOKEN",
    )
    run_parser.add_argument(
        "--env-package",
        action="append",
        default=[],
        metavar="DIR",
        help="local verifiers environment package to install into prime-rl",
    )
    run_parser.add_argument(
        "--pip",
        action="append",
        default=[],
        metavar="SPEC",
        help="extra package to install into prime-rl",
    )
    run_parser.add_argument("--image", default=PRIME_RL_IMAGE)
    run_parser.add_argument(
        "--data", help="Chalk dataset to attach instead of the placeholder"
    )
    run_parser.add_argument(
        "--detach", action="store_true", help="exit once the run is submitted"
    )
    run_parser.add_argument(
        "--timeout", type=float, help="seconds to wait before canceling the run"
    )

    commands.add_parser("wait", help="follow an existing run").add_argument("run_id")
    commands.add_parser("cancel", help="cancel an existing run").add_argument("run_id")

    # Split off prime-rl's arguments by hand: argparse's own `--` handling varies across
    # Python versions once subparsers are involved.
    argv = sys.argv[1:] if argv is None else list(argv)
    passthrough: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, passthrough = argv[:split], argv[split + 1 :]
    options = parser.parse_args(argv)
    if options.environment:
        os.environ["CHALK_ENVIRONMENT"] = options.environment
    if options.api_server:
        os.environ["CHALK_API_SERVER"] = options.api_server

    from chalkcompute._connect import ConnectClient
    from chalkcompute._training import TrainingRunHandle

    if options.command == "train":
        run = train(
            options.config,
            name=options.name,
            gpu=options.gpu,
            cpu=options.cpu,
            memory=options.memory,
            max_steps=options.max_steps,
            args=passthrough,
            env=dict(item.split("=", 1) for item in options.env),
            secrets=options.secret,
            pip=options.pip,
            env_packages=options.env_package,
            image=options.image,
            data=options.data,
        )
        print(f"submitted training run {run.run_id}", flush=True)
        if url := dashboard_url(run):
            print(url, flush=True)
        return 0 if options.detach else _follow(run, options.timeout)

    run = TrainingRunHandle(options.run_id, ConnectClient())
    if options.command == "cancel":
        run.cancel()
        print(f"canceled run {run.run_id}")
        return 0
    return _follow(run, None)


def _follow(run: TrainingRunHandle, timeout: float | None) -> int:
    from chalkcompute._training import TrainingError

    try:
        run.wait(poll_interval=15, timeout=timeout)
    except TrainingError as exc:
        print(exc)
        return 1
    except (TimeoutError, KeyboardInterrupt):
        run.cancel()
        print(f"canceled run {run.run_id}")
        return 1
    print(f"run {run.run_id} completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
