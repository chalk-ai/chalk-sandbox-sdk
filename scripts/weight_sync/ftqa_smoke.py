"""Disposable real-volume/vLLM smoke on one FTQA A100. Never runs on import.

Run with the SDK environment, e.g. PYTHONPATH=. python scripts/weight_sync/ftqa_smoke.py.
Model artifacts remain remote. Only source and benchmark evidence reach the Mac.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODELS = {
    "small": ("Qwen/Qwen3-0.6B", "c1899de289a04d12100db370d81485cdf75e47ca"),
    "8b": ("Qwen/Qwen3-8B", "b968826d9c46dd6066d109eabc6255188de91218"),
}


def load_env(path):
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if key.startswith("CHALK_"):
                os.environ[key] = value.strip().strip('"').strip("'")


def execute(sandbox, command, *, timeout=120):
    result = sandbox.exec("bash", "-lc", command, timeout_secs=timeout)
    if result.error is not None or result.exit_code not in (0, None):
        raise RuntimeError(
            f"Remote command failed (exit={result.exit_code}, error={result.error}): {result.stderr_text[-2000:]}"
        )
    return result.stdout_text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=MODELS, default="small")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / ".env",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=ROOT,
        help="SDK checkout integrating the publisher and serving feature branches",
    )
    parser.add_argument("--lifetime-seconds", type=int, default=3600)
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    weights_source = source_root / "chalkcompute" / "_weights.py"
    if (
        not weights_source.exists()
        or "class WeightPublisher:" not in weights_source.read_text()
    ):
        parser.error("--source-root must include the weight publisher implementation")
    if not (source_root / "chalkcompute/weights/serving/__main__.py").exists():
        parser.error("--source-root must include the serving runtime")
    if not 120 <= args.lifetime_seconds <= 3600:
        parser.error("Lifetime must be between 120 and 3600 seconds")
    load_env(args.env_file)
    import chalkcompute as cc
    from chalkcompute._volume import VolumeNotFoundError

    run_id = f"weight-sync-{args.model}-{uuid.uuid4().hex[:10]}"
    out = args.out or ROOT / ".artifacts" / run_id
    out.mkdir(parents=True, exist_ok=False)
    model, revision = MODELS[args.model]
    record = {
        "run_id": run_id,
        "model": model,
        "revision": revision,
        "status": "running",
        "sandbox_released": False,
        "volume_deleted": False,
        "started_at": time.time(),
    }

    def save():
        (out / "launcher.json").write_text(json.dumps(record, indent=2))

    save()
    # These credentials go directly to this disposable sandbox and never to a
    # source archive, shell argument, process command string, or result artifact.
    env_keys = {
        "CHALK_API_SERVER",
        "CHALK_CLIENT_ID",
        "CHALK_CLIENT_SECRET",
        "CHALK_ACTIVE_ENVIRONMENT",
        "CHALK_ENVIRONMENT",
        "CHALK_ENVIRONMENT_ID",
    }
    remote_env = {key: value for key, value in os.environ.items() if key in env_keys}
    volume = cc.Volume(run_id)
    sandbox = cc.Sandbox(
        name=run_id,
        image="vllm/vllm-openai:v0.33.0",
        entrypoint=["sleep", "infinity"],
        execution_target=cc.ExecutionBackend.SERVERLESS,
        compute_class=cc.ComputeClass.HOST,
        cpu="16",
        memory="128Gi",
        gpu="nvidia-a100-80gb:1",
        lifetime=f"{args.lifetime_seconds}s",
        env=remote_env,
        volumes=[
            cc.VolumeMount(name=run_id, mount_path="/chalk/weights", ref_name="main")
        ],
    )
    allocation_deadline = time.monotonic() + args.lifetime_seconds
    try:
        volume.reload()
        with tempfile.TemporaryDirectory(prefix="weight-sync-source-") as staging:
            archive = Path(staging) / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for path in sorted((source_root / "chalkcompute").rglob("*")):
                    if (
                        path.is_file()
                        and path.suffix in {".py", ".pyi", ".typed"}
                        and "__pycache__" not in path.parts
                    ):
                        tar.add(path, arcname=path.relative_to(source_root))
            import hashlib
            import shutil

            shutil.copyfile(archive, out / "source.tar.gz")

            record["source_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            print(
                json.dumps(
                    {"phase": "claiming_a100", "run_id": run_id, "out": str(out)}
                ),
                flush=True,
            )
            sandbox.run(timeout=600)
            record["sandbox_id"] = sandbox.id
            save()
            print(
                json.dumps(
                    {
                        "phase": "claimed_a100",
                        "sandbox_id": sandbox.id,
                        "run_id": run_id,
                    }
                ),
                flush=True,
            )
            execute(sandbox, "mkdir -p /test/results")
            sandbox.fs.add_file(archive, "/test/source.tar.gz")
            sandbox.fs.add_file(
                Path(__file__).with_name("remote_smoke.py"), "/test/remote_smoke.py"
            )
        # Snapshot installed constraints before any dependency install, preserving
        # the proven engine/torch combination. Add only the SDK and codec deps.
        setup = r"""set -euo pipefail
mkdir -p /test/results /test/source
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader > /test/results/gpu.txt
python3 -m pip list --format=freeze > /test/constraints.txt
cp /test/constraints.txt /test/results/packages-before.txt
python3 -m pip install --no-cache-dir --constraint /test/constraints.txt chalkcompute aiohttp zstandard xxhash safetensors > /test/results/install.log 2>&1
python3 -m pip list --format=freeze > /test/results/packages-after.txt
tar -xzf /test/source.tar.gz -C /test/source
python3 - <<'INNER'
import chalkcompute,pathlib,shutil
source=pathlib.Path('/test/source/chalkcompute')
target=pathlib.Path(chalkcompute.__file__).parent
shutil.copytree(source,target,dirs_exist_ok=True)
INNER
# Known prerequisite until the independent hypervisor shm fix is deployed.
stat -c '%a %U:%G' /dev/shm > /test/results/shm-before.txt
chmod 1777 /dev/shm
export LD_LIBRARY_PATH="/usr/local/nvidia/lib64:${LD_LIBRARY_PATH:-}"
export PYTHONUNBUFFERED=1
python3 /test/remote_smoke.py __ARGS__ > /test/results/driver.log 2>&1
"""
        remote_args = shlex.join(
            ["--stream", run_id, "--model", model, "--revision", revision]
        )
        sandbox.fs.add_file(
            setup.replace("__ARGS__", remote_args).encode(), "/test/run.sh", mode=0o755
        )
        execute(
            sandbox,
            "nohup bash /test/run.sh > /test/results-setup.log 2>&1 & echo $! > /test/driver.pid",
        )
        deadline = allocation_deadline - 120
        seen = None
        while time.monotonic() < deadline:
            status = execute(
                sandbox, "cat /test/results/result.json 2>/dev/null || true", timeout=30
            )
            if status and status != seen:
                seen = status
                report = json.loads(status)
                print(json.dumps(report["phases"][-1]), flush=True)
                if report["status"] in {"passed", "failed"}:
                    record["remote_status"] = report["status"]
                    break
            alive = execute(
                sandbox,
                'kill -0 "$(cat /test/driver.pid)" 2>/dev/null && echo alive || true',
                timeout=30,
            ).strip()
            if not alive:
                record["remote_status"] = "driver_exited"
                break
            time.sleep(10)
        else:
            raise TimeoutError("Smoke exceeded its allocation budget")
        if record.get("remote_status") != "passed":
            raise RuntimeError(f"Smoke failed: {record.get('remote_status')}")
        record["status"] = "passed"
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        record["finished_at"] = time.time()
        if sandbox.id is None:
            info = sandbox.info
            if info is not None:
                record["sandbox_id"] = info.id
                record["sandbox_released"] = info.status in {
                    "terminated",
                    "failed",
                    "error",
                }
                record["terminal_status"] = info.status
            else:
                record["sandbox_released"] = True  # Creation never claimed a resource.
        if sandbox.id is not None:
            record["sandbox_id"] = sandbox.id
            try:
                # Pull only named lightweight evidence files, never credentials
                # or downloaded model checkpoints.
                for name in (
                    "result.json",
                    "driver.log",
                    "supervisor.log",
                    "metrics.prom",
                    "gpu.txt",
                    "install.log",
                    "packages-before.txt",
                    "packages-after.txt",
                    "shm-before.txt",
                ):
                    try:
                        content = sandbox.fs.read_bytes(
                            f"/test/results/{name}", timeout_secs=30
                        )
                        (out / name).write_bytes(content)
                    except Exception as error:
                        record.setdefault("artifact_errors", []).append(
                            f"{name}: {type(error).__name__}"
                        )
            finally:
                sandbox_id = sandbox.id
                try:
                    sandbox.terminate()
                    for attempt in range(12):
                        info = sandbox.refresh()
                        if info.status in {"terminated", "failed", "error"}:
                            record["sandbox_released"] = True
                            record["terminal_status"] = info.status
                            break
                        time.sleep(5)
                    if not record["sandbox_released"]:
                        raise RuntimeError("Sandbox termination was not confirmed")
                except Exception as error:
                    record["cleanup_error"] = f"{type(error).__name__}: {error}"
                    print(
                        json.dumps(
                            {
                                "phase": "CLEANUP_FAILED",
                                "sandbox_id": sandbox_id,
                                "error": type(error).__name__,
                            }
                        ),
                        flush=True,
                    )
        try:
            volume.delete()
            try:
                cc.Volume(
                    run_id, client=volume._client, create_if_missing=False
                ).reload()
            except VolumeNotFoundError:
                record["volume_deleted"] = True
        except Exception as error:
            record["volume_cleanup_error"] = f"{type(error).__name__}: {error}"
        save()
        print(
            json.dumps(
                {
                    "phase": "cleanup",
                    "sandbox_id": record.get("sandbox_id"),
                    "sandbox_released": record["sandbox_released"],
                    "volume_deleted": record["volume_deleted"],
                    "artifacts": str(out),
                }
            ),
            flush=True,
        )
        if not record["sandbox_released"] or not record["volume_deleted"]:
            raise RuntimeError(f"Cleanup incomplete; inspect {out / 'launcher.json'}")


if __name__ == "__main__":
    main()
