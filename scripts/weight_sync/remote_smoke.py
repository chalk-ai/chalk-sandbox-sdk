"""Run inside the disposable FTQA sandbox; emit only benchmark evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

import torch
from huggingface_hub import snapshot_download
from safetensors.torch import load_file

from chalkcompute._weights_format import WeightManifest, version_dir
from chalkcompute.weights import WeightStream

OUT = Path("/test/results")
OUT.mkdir(parents=True, exist_ok=True)
RESULT: dict = {"phases": [], "status": "running"}


def phase(name, **extra):
    item = {"phase": name, "time": time.time(), **extra}
    if torch.cuda.is_initialized():
        item["trainer_gpu_memory"] = {
            "allocated": torch.cuda.memory_allocated(),
            "reserved": torch.cuda.memory_reserved(),
            "peak_allocated": torch.cuda.max_memory_allocated(),
            "free_total": list(torch.cuda.mem_get_info()),
        }
    if name in {
        "baseline",
        "dense_changed",
        "dense_restore_exact",
        "sparse_changed_verified",
        "sparse_restore_exact",
        "backward_pin_exact",
        "unpin_exact",
        "fresh_load_equivalent",
    }:
        with urlopen("http://127.0.0.1:8000/chalk/metrics", timeout=5) as response:
            text = response.read().decode()
        (OUT / f"metrics-{name}.prom").write_text(text)
        item["sync_metrics"] = {
            line.split()[0]: float(line.split()[1])
            for line in text.splitlines()
            if line and not line.startswith("#")
        }
    RESULT["phases"].append(item)
    (OUT / "result.json").write_text(json.dumps(RESULT, indent=2))
    print(json.dumps(item), flush=True)


def http(path, body=None, *, engine=False, timeout=120):
    request = Request(
        f"http://127.0.0.1:{8001 if engine else 8000}" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response), dict(response.headers)


def wait_version(version, process, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Supervisor exited ({process.returncode})")
        try:
            data, headers = http("/chalk/health", timeout=5)
            if data["applied_version"] == version:
                assert headers.get("x-chalk-weight-version") == str(version)
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise TimeoutError(f"Policy did not reach version {version}")


def sample():
    data, headers = http(
        "/v1/completions",
        {
            "model": "policy",
            "prompt": "The capital of France is",
            "temperature": 0,
            "seed": 0,
            "max_tokens": 16,
            "ignore_eos": True,
        },
    )
    return data["choices"][0]["text"], headers["x-chalk-policy-version"]


def tensor_hash(tensor):
    raw = (
        tensor.detach()
        .contiguous()
        .reshape(-1)
        .view(torch.uint8)
        .cpu()
        .numpy()
        .tobytes()
    )
    return hashlib.sha256(raw).hexdigest()


def audit(name, expected):
    result, _ = http(
        "/collective_rpc", {"method": "ws_tensor_hashes", "args": [[name]]}, engine=True
    )
    assert result["results"] and all(
        rank[name] == tensor_hash(expected) for rank in result["results"]
    ), result
    return result


def launch(model_dir):
    env = {**os.environ, "CHALK_WEIGHT_SYNC_POLL_S": ".2", "PYTHONUNBUFFERED": "1"}
    log = (OUT / "supervisor.log").open("ab", buffering=0)
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "chalkcompute.weights.serving",
            "--weights",
            str(model_dir),
            "--startup-timeout",
            "900",
            "--",
            "--served-model-name",
            "policy",
            "--dtype",
            "bfloat16",
            "--max-model-len",
            "2048",
            "--gpu-memory-utilization",
            ".3",
            "--enforce-eager",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


def run(args):
    import vllm

    phase(
        "inventory",
        torch=torch.__version__,
        vllm=vllm.__version__,
        model=args.model,
        revision=args.revision,
    )
    assert vllm.__version__.startswith("0.33."), vllm.__version__
    stream = WeightStream(args.stream, args.model).create()
    phase("seed_begin")
    started = time.monotonic()
    assert stream.seed_from_hf(args.model, args.revision) == 0
    phase("seed_complete", elapsed_s=time.monotonic() - started)
    snapshot = Path(
        snapshot_download(
            args.model,
            revision=args.revision,
            local_files_only=True,
            allow_patterns=[
                "*.safetensors",
                "*.json",
                "*.model",
                "*.tiktoken",
                "*.txt",
            ],
        )
    )
    state = {}
    for file in sorted(snapshot.glob("*.safetensors")):
        state.update(
            {name: tensor.to("cuda") for name, tensor in load_file(str(file)).items()}
        )
    publisher = stream.publisher()
    phase("resume_begin")
    publisher.resume(state)
    phase("resume_complete")

    def publish_state(state, step):
        started = time.monotonic()
        version = publisher.publish(state, step=step)
        RESULT.setdefault("publications", []).append(
            {"version": version, "publish_ms": (time.monotonic() - started) * 1000}
        )
        return version

    norm_name = "model.norm.weight"
    original = state[norm_name].clone()
    process = launch(Path("/chalk/weights"))
    try:
        phase("engine_starting", supervisor_pid=process.pid)
        wait_version(0, process, timeout=900)
        baseline, version = sample()
        assert version == "0"
        audit(norm_name, original)
        phase("baseline", output=baseline, policy_version=version)
        state[norm_name].zero_()
        started = time.monotonic()
        assert publish_state(state, step=1) == 1
        wait_version(1, process)
        changed, version = sample()
        visible_s = time.monotonic() - started
        assert version == "1" and changed != baseline, (changed, baseline, version)
        audit(norm_name, state[norm_name])
        phase(
            "dense_changed",
            output=changed,
            policy_version=version,
            publish_to_visible_s=visible_s,
        )
        state[norm_name].copy_(original)
        assert publish_state(state, step=2) == 2
        wait_version(2, process)
        restored, version = sample()
        assert restored == baseline and version == "2"
        audit(norm_name, original)
        phase("dense_restore_exact", policy_version=version)
        state[norm_name].reshape(-1)[0] = original.reshape(-1)[0] + 100
        started = time.monotonic()
        assert publish_state(state, step=3) == 3
        wait_version(3, process)
        sparse, version = sample()
        visible_s = time.monotonic() - started
        assert version == "3"
        audit(norm_name, state[norm_name])
        manifest = WeightManifest.from_json(
            stream.volume.read_file(f"{version_dir(3)}/manifest.json")
        )
        assert len(manifest.tensors) == 1 and manifest.tensors[0].encoding == "sparse"
        phase(
            "sparse_changed_verified",
            policy_version=version,
            output=sparse,
            bytes=sum(e.length for e in manifest.tensors),
            tensor_density=1 / original.numel(),
            changed_tensor=norm_name,
            changed_elements=1,
            full_model_elements=sum(t.numel() for t in state.values()),
            full_model_density=1 / sum(t.numel() for t in state.values()),
            publish_to_visible_s=visible_s,
        )
        # Audit against a fresh load of the exact nontrivial sparse version, using
        # this same GPU sequentially rather than allocating a second replica.
        phase("fresh_audit_materialize_begin")
        audit_dir = stream.materialize(3, "/test/audit-v3")
        phase("fresh_audit_materialize_complete")
        state[norm_name].copy_(original)
        assert publish_state(state, step=4) == 4
        wait_version(4, process)
        restored, version = sample()
        assert restored == baseline and version == "4"
        audit(norm_name, original)
        phase("sparse_restore_exact", policy_version=version)
        stream.pin(1)
        wait_version(1, process)
        pinned, version = sample()
        assert pinned == changed and version == "1"
        phase("backward_pin_exact", policy_version=version)
        stream.unpin()
        wait_version(4, process)
        assert sample() == (baseline, "4")
        phase("unpin_exact")
        with urlopen("http://127.0.0.1:8000/chalk/metrics") as response:
            (OUT / "metrics.prom").write_bytes(response.read())
        stop(process)
        phase("fresh_engine_starting")
        # A local stream keyframe makes the ordinary boot path start directly
        # from the independently materialized v3 checkpoint.
        local_root = Path("/test/fresh-stream")
        local_root.mkdir()
        fresh_dir = local_root / "v000000"
        fresh_dir.mkdir()
        for file in audit_dir.iterdir():
            if file.is_file():
                os.link(file, fresh_dir / file.name)
        payload = fresh_dir / "model.safetensors"
        import xxhash

        digest = xxhash.xxh3_128()
        with payload.open("rb") as file:
            while chunk := file.read(4 * 1024 * 1024):
                digest.update(chunk)
        from dataclasses import replace

        base = WeightManifest.from_json(
            stream.volume.read_file("v000000/manifest.json")
        )
        entries = tuple(
            replace(
                entry,
                file="model.safetensors",
                offset=0,
                length=payload.stat().st_size,
                checksum=digest.hexdigest(),
            )
            for entry in base.tensors
        )
        fresh_manifest = replace(base, tensors=entries)
        (fresh_dir / "manifest.json").write_bytes(fresh_manifest.to_json())
        from chalkcompute._weights_format import pointer_json

        (local_root / "CURRENT").write_bytes(pointer_json(0))
        process = launch(local_root)
        wait_version(0, process, timeout=900)
        fresh, _ = sample()
        assert fresh == sparse, (fresh, sparse)
        phase("fresh_load_equivalent", output=fresh)
        RESULT["status"] = "passed"
        phase("complete")
    finally:
        stop(process)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stream", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    try:
        run(args)
    except BaseException as error:
        RESULT["status"] = "failed"
        RESULT["error"] = f"{type(error).__name__}: {error}"
        phase("failed")
        raise


if __name__ == "__main__":
    main()
