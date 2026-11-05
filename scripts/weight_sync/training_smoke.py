"""Bounded real cc.training + separate live serving A100 integration smoke.

Default mode builds/prepares only CPU image and dataset; --execute opts in to
two A100s. Credentials never enter source archives, arguments, or evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

from ftqa_smoke import MODELS, execute, load_env

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = [
    {
        "question": "Natalia sold clips to 48 of her friends in April, and then she sold half as many clips in May. How many clips did Natalia sell altogether in April and May?",
        "answer": "Natalia sold 48/2 = 24 clips in May. Natalia sold 48+24 = 72 clips altogether in April and May. #### 72",
        "source": "openai/gsm8k:main/train:0",
    },
    {
        "question": "Weng earns $12 an hour for babysitting. Yesterday, she just did 50 minutes of babysitting. How much did she earn?",
        "answer": "Weng earns 12/60 = $0.2 per minute. Working 50 minutes, she earned 0.2 x 50 = $10. #### 10",
        "source": "openai/gsm8k:main/train:1",
    },
]


def make_image(source_root, staging):
    """Build CPU-side, retaining the released Linux native extension."""
    import chalkcompute as cc

    archive = staging / "weight-sdk-overlay.tar.gz"
    package = source_root / "chalkcompute"
    paths = [
        package / name
        for name in (
            "_weights.py",
            "_weights_format.py",
            "_weights_codec.py",
            "_training.py",
            "_training_entrypoint.py",
        )
    ]
    paths.extend(sorted((package / "weights").rglob("*.py")))
    with tarfile.open(archive, "w:gz") as tar:
        for path in paths:
            if "__pycache__" not in path.parts:
                tar.add(path, arcname=path.relative_to(source_root))
    if archive.stat().st_size > 32 * 1024:
        raise ValueError("Inline SDK overlay exceeds image copy budget")
    overlay = "import pathlib,tarfile,chalkcompute; tarfile.open('/tmp/weight-sdk-overlay.tar.gz').extractall(pathlib.Path(chalkcompute.__file__).parent.parent, filter='data')"
    return (
        cc.Image.base("vllm/vllm-openai:v0.33.0")
        .run_commands(
            "ln -sf $(command -v python3) /usr/local/bin/python && python -m pip list --format=freeze > /tmp/constraints.txt"
        )
        .run_commands(
            "python3 -m pip install --no-cache-dir --constraint /tmp/constraints.txt chalkcompute==2.13.16 aiohttp zstandard xxhash safetensors polars"
        )
        .add_local_file(archive, "/tmp/weight-sdk-overlay.tar.gz", strategy="copy")
        .add_local_file(
            Path(__file__).with_name("serving_entrypoint.py"),
            "/test/serving_entrypoint.py",
            strategy="copy",
        )
        .run_commands("python3 -c " + shlex.quote(overlay))
        .run_commands(
            'python -c "import chalkcompute._native, chalkcompute._training_entrypoint"'
        )
        .run_commands(
            "python3 -c "
            + shlex.quote(
                "import shutil; assert shutil.which('python') and shutil.which('python3'); import chalkcompute._training_entrypoint; import torch,vllm,polars,pyarrow,aiohttp,zstandard,xxhash; import chalkcompute._native; from chalkcompute._weights import WeightPublisher; import chalkcompute.weights.serving; assert vllm.__version__ == '0.33.0'; assert torch.__version__ == '2.14.0+cu130'; print('validated', torch.__version__, vllm.__version__)"
            )
        )
    )


def overlay_members(path):
    with tarfile.open(path, "r:gz") as archive:
        return {
            member.name: hashlib.sha256(archive.extractfile(member).read()).hexdigest()
            for member in archive.getmembers()
            if member.isfile()
        }


def retain_trainer_evidence(guard, *, timeout=15):
    """Best-effort child process; native I/O cannot stall resource release."""
    code = """
import json, sys
from pathlib import Path
import chalkcompute as cc
from chalkcompute._volume import VolumeFileNotFoundError, VolumeNotFoundError
try:
    data = cc.Volume(sys.argv[1], create_if_missing=False).read_file('evidence/trainer.json')
except (VolumeFileNotFoundError, VolumeNotFoundError):
    raise SystemExit(0)
assert len(data) <= 1024 * 1024, 'Trainer evidence exceeds bounded report size'
json.loads(data)
Path(sys.argv[2]).write_bytes(data)
"""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                guard.name,
                str(guard.out / "trainer-final.json"),
            ],
            timeout=timeout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        status = "finished" if result.returncode == 0 else "unavailable"
    except subprocess.TimeoutExpired:
        status = "timed_out"
    except OSError:
        status = "unavailable"
    guard.record["evidence_retention"] = status
    guard.save()


def release_compute_then_retain(guard, *, timeout=15):
    guard.cleanup_compute()
    retain_trainer_evidence(guard, timeout=timeout)


class ResourceGuard:
    """Persist IDs immediately, recover ambiguous submissions by unique name."""

    def __init__(self, name, out, record, kube_context, fallback_context):
        self.name, self.out, self.record = name, out, record
        self.sandboxes = []
        self.lock = threading.Lock()
        self.run_id = record.get("training_run_id")
        self.dataset_id = record.get("dataset_id")
        self.volume = None
        self.groups = []
        self.kube_context = kube_context
        self.kube_contexts = record.get("kube_contexts") or [
            kube_context,
            fallback_context,
        ]
        self.cleanup_lock = threading.Lock()
        self.claim_deadline = None

    def check_budget(self):
        if (
            (self.out / "watchdog-expired").exists()
            or self.record.get("watchdog_expired")
            or (
                self.claim_deadline is not None
                and time.monotonic() >= self.claim_deadline
            )
        ):
            raise TimeoutError("GPU budget expired; further submissions are refused")

    def save(self):
        with self.lock:
            try:
                temporary = self.out / "launcher.tmp"
                temporary.write_text(json.dumps(self.record, indent=2))
                temporary.replace(self.out / "launcher.json")
            except OSError as error:
                # Resource release must still happen if local evidence storage
                # fills after allocation; the detached guard can recover names.
                print(
                    f"Ledger write failed: {type(error).__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )

    def recover_run(self, client):
        from chalkcompute._gen.chalk.server.v1 import (
            training_runs_connect as tc,
            training_runs_pb as tp,
        )

        response = client.call(
            client.stub(tc.TrainingRunServiceClientSync).list_training_runs,
            tp.ListTrainingRunsRequest(
                limit=100, filters=tp.ListTrainingRunsFilters(name=self.name)
            ),
        )
        runs = [run for run in response.training_runs if run.name == self.name]
        if len(runs) > 1:
            raise RuntimeError(
                "Unique training run name unexpectedly matched multiple runs"
            )
        if runs:
            self.run_id = runs[0].id
            self.record["training_run_id"] = self.run_id
            self.save()
        return runs

    def stop_training(self):
        from chalkcompute._connect import ConnectClient
        from chalkcompute._gen.chalk.container.v1 import (
            service_connect as cc,
            service_pb as cp,
        )
        from chalkcompute._gen.chalk.server.v1 import (
            training_runs_connect as tc,
            training_runs_pb as tp,
        )

        client = ConnectClient()
        if self.run_id is None:
            self.recover_run(client)
        if self.run_id is None:
            self.record["training_container_released"] = True
            return
        training = client.stub(tc.TrainingRunServiceClientSync)
        run = client.call(
            training.get_training_run,
            tp.GetTrainingRunRequest(training_run_id=self.run_id),
        ).training_run
        if run and run.status not in {
            tp.TrainingRunStatus.COMPLETED,
            tp.TrainingRunStatus.FAILED,
            tp.TrainingRunStatus.CANCELED,
        }:
            client.call(
                training.cancel_training_run,
                tp.CancelTrainingRunRequest(training_run_id=self.run_id),
            )
        # Backend creates the training container with container ID == run UUID.
        containers = client.stub(cc.ContainerServiceClientSync)
        response = client.call(
            containers.get_container,
            cp.GetContainerRequest(id=self.run_id, include_stopped=True),
        )
        if response.container is None:
            raise RuntimeError("Cannot verify training container absence")
        if response.container.status.lower() not in {
            "succeeded",
            "failed",
            "terminated",
            "error",
        }:
            client.call(
                containers.stop_container,
                cp.StopContainerRequest(id=self.run_id, grace_period_seconds=10),
            )
        for _ in range(24):
            container = client.call(
                containers.get_container,
                cp.GetContainerRequest(id=self.run_id, include_stopped=True),
            ).container
            if container and container.status.lower() in {
                "succeeded",
                "failed",
                "terminated",
                "error",
            }:
                self.verify_training_absent()
                self.record["training_container_released"] = True
                self.record["training_container_terminal_status"] = container.status
                self.save()
                return
            time.sleep(5)
        raise RuntimeError("Training container termination unconfirmed")

    def capture_training_resource(self):
        return self.capture_container_resource(self.run_id, "training_resources")

    def capture_container_resource(self, container_id, resource_key):
        """Record the real CR UID; API terminal status does not prove finalization."""
        if not container_id:
            return
        found = []
        for context in self.kube_contexts:
            for item in self.kube_items(
                "chalkcontainers", name="cc-" + container_id, context=context
            ):
                metadata = item["metadata"]
                if metadata["namespace"] not in self.record["expected_namespaces"]:
                    raise RuntimeError(
                        "Container CR namespace differs from environment"
                    )
                found.append(
                    {
                        "context": context,
                        "namespace": metadata["namespace"],
                        "uid": metadata["uid"],
                        "name": metadata["name"],
                        "phase": item.get("status", {}).get("phase"),
                        "message": item.get("status", {}).get("message"),
                    }
                )
        if found:
            self.record[resource_key] = found
            self.save()
        return found

    def verify_training_absent(self):
        self.verify_container_absent(self.run_id, "training_resources")
        self.record["training_cr_verified_absent"] = True
        self.save()

    def verify_container_absent(self, container_id, resource_key):
        self.capture_container_resource(container_id, resource_key)
        # The container service may retain a terminal CR. Delete only our recorded
        # immutable UID and let the controller finalizer perform runtime cleanup.
        for resource in self.record.get(resource_key, []):
            options = {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": resource["uid"]},
            }
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
                json.dump(options, handle)
                handle.flush()
                path = (
                    "/apis/dataplane.chalk.ai/v1alpha1/namespaces/"
                    + resource["namespace"]
                    + "/chalkcontainers/"
                    + resource["name"]
                )
                result = subprocess.run(
                    [
                        "kubectl",
                        "--context",
                        resource["context"],
                        "delete",
                        "--raw",
                        path,
                        "-f",
                        handle.name,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
                if result.returncode and "NotFound" not in result.stderr:
                    raise RuntimeError(
                        "Owned container CR delete failed: " + result.stderr
                    )

        for _ in range(36):
            remaining = []
            for context in self.kube_contexts:
                remaining.extend(
                    self.kube_items(
                        "chalkcontainers", name="cc-" + container_id, context=context
                    )
                )
            resources = self.record.get(resource_key, [])
            for resource in resources:
                for pod in self.kube_items("pods", context=resource["context"]):
                    if any(
                        owner.get("uid") == resource["uid"]
                        for owner in pod["metadata"].get("ownerReferences", [])
                    ):
                        remaining.append(pod)
            if not remaining:
                self.record[resource_key + "_verified_absent"] = True
                self.save()
                return
            time.sleep(5)
        raise RuntimeError("Container CR/owned pods remain after API stop")

    def stop_sandbox(self, sandbox):
        import chalkcompute as cc
        from chalkcompute._sandbox import SandboxNotFoundError

        sandbox_id = sandbox.id
        try:
            if sandbox_id is None:
                sandbox = cc.Sandbox.from_name(sandbox._name)
                sandbox_id = sandbox.id
            sandbox.terminate()
            for _ in range(24):
                info = sandbox.refresh()
                if info.status in {"terminated", "failed", "error"}:
                    self.record.setdefault("released_sandboxes", {})[sandbox_id] = (
                        info.status
                    )
                    self.save()
                    return
                time.sleep(5)
        except SandboxNotFoundError:
            self.record.setdefault("released_sandboxes", {})[sandbox_id] = "not_found"
            self.save()
            return
        raise RuntimeError(f"Sandbox {sandbox_id} termination unconfirmed")

    def kube_items(self, resource, **kwargs):
        command = [
            "kubectl",
            "--context",
            kwargs.get("context", self.kube_context),
            "get",
            resource,
            "-A",
            "-o",
            "json",
            "--request-timeout=30s",
        ]
        if "name" in kwargs:
            command += ["--field-selector", "metadata.name=" + kwargs["name"]]
        if "owner" in kwargs:
            command += ["-l", "chalk.ai/scaling-group-owner=" + kwargs["owner"]]
        result = subprocess.run(
            command, check=True, text=True, capture_output=True, timeout=45
        )
        return json.loads(result.stdout)["items"]

    def stop_group(self, name, group):
        import chalkcompute as cc

        recorded_id = self.record.get("group_ids", {}).get(name)
        if recorded_id in self.record.get("released_groups", []):
            return
        if group.id is None:
            group = cc.ScalingGroup.from_name(name)
        group_id = group.id
        crs = []
        for context in self.kube_contexts:
            try:
                found = self.kube_items(
                    "chalkscalinggroups", name="sg-" + group_id, context=context
                )
            except (subprocess.SubprocessError, OSError):
                continue
            if found:
                crs = found
                break
        if not crs:
            # Still issue the API deletion; absence in the wrong kube context
            # must never be mistaken for evidence that a live group was freed.
            group.delete()
            if group_id in self.record.get("released_groups", []):
                return
            raise RuntimeError(
                "Could not locate exact ScalingGroup CR for verification: " + group_id
            )
        owners = [item["metadata"]["uid"] for item in crs]
        if any(
            item["metadata"]["namespace"]
            not in self.record.get(
                "expected_namespaces", [self.record["environment_id"]]
            )
            for item in crs
        ):
            group.delete()
            raise RuntimeError(
                "ScalingGroup CR namespace differs from authenticated environment"
            )
        replica_names = [
            item["metadata"]["name"]
            for owner in owners
            for item in self.kube_items("chalkcontainers", owner=owner, context=context)
        ]
        self.record.setdefault("group_resources", {})[name] = {
            "id": group_id,
            "owners": owners,
            "replicas": replica_names,
            "context": context,
            "namespace": crs[0]["metadata"]["namespace"],
        }
        self.save()
        group.delete()
        for _ in range(36):
            crs = self.kube_items(
                "chalkscalinggroups", name="sg-" + group_id, context=context
            )
            children = [
                item
                for owner in owners
                for item in self.kube_items(
                    "chalkcontainers", owner=owner, context=context
                )
            ]
            if not crs and not children:
                self.record.setdefault("released_groups", []).append(group_id)
                self.save()
                return
            time.sleep(5)
        raise RuntimeError("ScalingGroup CR/underlying replicas remain: " + group_id)

    def cleanup_compute(self):
        with self.cleanup_lock:
            self._cleanup_compute()

    def _cleanup_compute(self):
        errors = []
        try:
            self.stop_training()
        except Exception as error:
            errors.append(f"training: {type(error).__name__}: {error}")
        for sandbox in self.sandboxes:
            try:
                self.stop_sandbox(sandbox)
            except Exception as error:
                errors.append(f"sandbox: {type(error).__name__}: {error}")
        for name, group in self.groups:
            try:
                self.stop_group(name, group)
            except Exception as error:
                errors.append(f"group: {type(error).__name__}: {error}")
        self.record["compute_cleanup_errors"] = errors
        self.save()
        if errors:
            raise RuntimeError("; ".join(errors))


def cleanup_data(guard):
    import chalkcompute as cc
    from chalkcompute._connect import ConnectClient
    from chalkcompute._gen.chalk.server.v1 import (
        datasets_connect as dc,
        datasets_pb as dp,
    )
    from chalkcompute._volume import VolumeNotFoundError

    errors = []
    client = ConnectClient()
    if guard.dataset_id:
        try:
            stub = client.stub(dc.DatasetMetadataServiceClientSync)
            client.call(
                stub.delete_dataset,
                dp.DeleteDatasetRequest(dataset_id=guard.dataset_id),
            )
            # GetDataset intentionally includes tombstoned metadata. The catalog
            # excludes deleted_at rows, so verify this exact ID through List.
            response = client.call(
                stub.list_datasets,
                dp.ListDatasetsRequest(
                    filters=dp.ListDatasetsFilters(ids=[guard.dataset_id])
                ),
            )
            guard.record["dataset_deleted"] = not response.datasets
            if not guard.record["dataset_deleted"]:
                raise RuntimeError("Dataset catalog tombstone unconfirmed")
        except Exception as error:
            errors.append(f"dataset: {type(error).__name__}: {error}")
    for name in (
        guard.name,
        "training-src-" + guard.name,
        "training-data-" + guard.name,
        "training-ckpt-" + guard.name,
    ):
        try:
            try:
                volume = cc.Volume(name, create_if_missing=False)
                volume.reload()
                volume.delete()
                cc.Volume(name, create_if_missing=False).reload()
            except VolumeNotFoundError:
                guard.record.setdefault("deleted_volumes", []).append(name)
            else:
                raise RuntimeError("Volume deletion unconfirmed: " + name)
        except Exception as error:
            errors.append(f"volume: {type(error).__name__}: {error}")
    return errors


def sample(group):
    response = group.call(
        "/v1/completions",
        json={
            "model": "policy",
            "prompt": "The capital of France is",
            "temperature": 0,
            "seed": 0,
            "max_tokens": 16,
            "ignore_eos": True,
        },
        timeout=120,
    )
    if response.status != 200:
        raise RuntimeError(
            f"Inference HTTP {response.status}: {response.content[:500]!r}"
        )
    return {
        "output": json.loads(response.content)["choices"][0]["text"],
        "version": response.headers["x-chalk-policy-version"],
    }


def serving_replica_identities(group, guard):
    result = []
    for context in guard.kube_contexts:
        groups = guard.kube_items(
            "chalkscalinggroups", name="sg-" + group.id, context=context
        )
        for item in groups:
            owner = item["metadata"]["uid"]
            children = guard.kube_items("chalkcontainers", owner=owner, context=context)
            result.extend(
                (
                    context,
                    owner,
                    child["metadata"]["name"],
                    child["metadata"]["uid"],
                )
                for child in children
            )
    assert len(result) == 1, f"Expected one owned serving replica: {result!r}"
    return result


def native_processes(replica, ssh_host):
    code = "\nimport json\nfrom pathlib import Path\nresult = []\nfor path in Path('/proc').glob('[0-9]*'):\n    try:\n        command = (path / 'cmdline').read_bytes()\n        api = b'-m\\0vllm.entrypoints.openai.api_server\\0' in command\n        worker = command.startswith((b'VLLM::EngineCore', b'VLLM::Worker'))\n        if api or worker:\n            started = (path / 'stat').read_text().rpartition(')')[2].split()[19]\n            result.append({'pid': int(path.name), 'start_ticks': started, 'kind': 'api' if api else 'worker'})\n    except (FileNotFoundError, ProcessLookupError):\n        pass\nassert any(item['kind'] == 'api' for item in result), 'No native API process found'\nassert any(item['kind'] == 'worker' for item in result), 'No native EngineCore/worker process found'\nprint(json.dumps(sorted(result, key=lambda item: item['pid'])))\n"
    command = [
        "sudo",
        "docker",
        "exec",
        "chalk-external-hypervisor",
        "runsc",
        "--root=/var/lib/chalk-hypervisor/runsc",
        "exec",
        replica,
        "python3",
        "-c",
        code,
    ]
    response = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            ssh_host,
            shlex.join(command),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(response.stdout)


def disconnect_probe(group, guard, ssh_host):
    """Close a real routed SSE request, then require the same healthy replica."""
    evidence = {"started_at": time.time(), "phase": "checking_identity"}
    guard.record["client_disconnect"] = evidence

    def save(**fields):
        evidence.update(fields)
        (guard.out / "client-disconnect.json").write_text(
            json.dumps(evidence, indent=2)
        )
        guard.save()

    save()

    before = serving_replica_identities(group, guard)

    processes_before = native_processes(before[0][2], ssh_host)
    save(
        replicas_before=before,
        native_processes_before=processes_before,
        phase="opening_stream",
    )
    group._client._ensure_auth()
    request = Request(
        group.web_url + "/v1/completions",
        data=json.dumps(
            {
                "model": "policy",
                "prompt": "Count from one to one hundred:",
                "temperature": 0,
                "max_tokens": 64,
                "ignore_eos": True,
                "stream": True,
            }
        ).encode(),
        headers={
            "Authorization": f"Bearer {group._client._access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        assert response.status == 200
        assert response.headers.get("x-chalk-policy-version") == "0"
        first = response.readline()
        assert first.startswith(b"data:") and b"[DONE]" not in first
    save(
        stream_closed_at=time.time(),
        first_sse_chunk_bytes=len(first),
        phase="checking_health",
    )
    # EOF is intentionally not consumed. The supervisor must finish native
    # generation without losing its admission token or restarting the replica.
    health = []
    for _ in range(20):
        guard.check_budget()
        response = group.call("/chalk/health", method="GET", timeout=10)
        assert response.status == 200, (
            f"Disconnected client lost readiness: {response.status}"
        )
        payload = json.loads(response.content)
        assert payload == {"applied_version": 0, "healthy": True}, payload
        assert response.headers["x-chalk-weight-version"] == "0"
        health.append(payload)
        time.sleep(0.5)
    after = serving_replica_identities(group, guard)
    assert before == after, "Serving replica restarted after client disconnect"
    processes_after = native_processes(after[0][2], ssh_host)
    assert processes_before == processes_after, (
        "Native processes restarted after client disconnect"
    )
    metrics_response = group.call("/chalk/metrics", method="GET", timeout=10)
    assert metrics_response.status == 200
    metrics = {
        line.split()[0]: float(line.split()[1])
        for line in metrics_response.content.decode().splitlines()
        if line and not line.startswith("#")
    }
    assert metrics["chalk_weight_sync_failures"] == 0
    assert metrics["chalk_weight_sync_updates"] == 0
    result = {
        **evidence,
        "completed_at": time.time(),
        "phase": "passed",
        "replicas_before": before,
        "replicas_after": after,
        "native_processes_before": processes_before,
        "native_processes_after": processes_after,
        "healthy_samples": health,
        "next_request": sample(group),
        "first_sse_chunk_bytes": len(first),
        "metrics": metrics,
    }
    assert result["next_request"]["version"] == "0"
    (guard.out / "client-disconnect.json").write_text(json.dumps(result, indent=2))
    return result


class Observer:
    """One bounded deterministic request at a time while real training runs."""

    def __init__(self, group, out):
        self.group, self.out = group, out
        self.stopped = threading.Event()
        self.rows = []
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        with (self.out / "visibility.jsonl").open("a", buffering=1) as file:
            while not self.stopped.is_set():
                row = {"request_started_at": time.time()}
                try:
                    row.update(sample(self.group))
                    row["response_received_at"] = time.time()
                    metrics = self.group.call(
                        "/chalk/metrics", method="GET", timeout=10
                    )
                    row["metrics"] = {
                        line.split()[0]: float(line.split()[1])
                        for line in metrics.content.decode().splitlines()
                        if line and not line.startswith("#")
                    }
                except Exception as error:
                    row["error"] = f"{type(error).__name__}: {error}"
                self.rows.append(row)
                file.write(json.dumps(row) + "\n")
                self.stopped.wait(0.5)

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=130)
        if self.thread.is_alive():
            raise RuntimeError("Visibility observer did not stop")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--prepared-image",
        type=Path,
        help="Reuse image.json from a completed CPU preparation with the same source checkout",
    )
    parser.add_argument(
        "--execute", action="store_true", help="Claim two A100s after CPU preparation"
    )
    parser.add_argument("--steps", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--cpu-threads", type=int, choices=range(1, 17), default=4)
    parser.add_argument(
        "--external-ssh-host",
        help="Known external HOST for scoped native process identity checks",
    )
    parser.add_argument(
        "--seed-on-serving",
        action="store_true",
        help="Acquire the seed within the planned serving A100 and its GPU deadline",
    )
    parser.add_argument("--budget-seconds", type=int, default=2400)
    parser.add_argument(
        "--kube-context",
        default="gke_chalk-serverless-stage_us-central1_chalk-svls-stage-us-central1-gke",
    )
    parser.add_argument(
        "--fallback-kube-context",
        default="gke_chalk-serverless-stage_us-west1_chalk-svls-c5ypez-us-west1-gke",
    )
    args = parser.parse_args()
    if args.execute and not args.external_ssh_host:
        parser.error(
            "--execute requires --external-ssh-host for native process identity proof"
        )
    if not 900 <= args.budget_seconds <= 3300:
        parser.error("GPU budget must be 900..3300 seconds")
    load_env(args.env_file)
    os.environ["CHALK_FUNCTION_STRIP_SOURCE"] = "1"
    import chalkcompute as cc
    from chalkcompute._dataset import DatasetClient
    from chalkcompute._connect import ConnectClient

    name = "weight-train-8b-" + uuid.uuid4().hex[:10]
    args.out.mkdir(parents=True, exist_ok=False)
    record = {
        "name": name,
        "launcher_pid": os.getpid(),
        "status": "preparing",
        "started_at": time.time(),
        "training_container_released": False,
        "kube_context": args.kube_context,
        "fallback_kube_context": args.fallback_kube_context,
    }
    guard = ResourceGuard(
        name, args.out, record, args.kube_context, args.fallback_kube_context
    )
    guard.save()
    model, revision = MODELS["8b"]
    env_keys = {
        "CHALK_API_SERVER",
        "CHALK_CLIENT_ID",
        "CHALK_CLIENT_SECRET",
        "CHALK_ACTIVE_ENVIRONMENT",
        "CHALK_ENVIRONMENT",
        "CHALK_ENVIRONMENT_ID",
    }
    remote_env = {key: value for key, value in os.environ.items() if key in env_keys}
    remote_env.update(
        {
            "LD_LIBRARY_PATH": "/usr/local/nvidia/lib64:/usr/local/cuda/lib64",
            "PYTHONUNBUFFERED": "1",
            # The guest sees host CPUs; cap libraries to an explicit process budget.
            "OMP_NUM_THREADS": str(args.cpu_threads),
            "MKL_NUM_THREADS": str(args.cpu_threads),
        }
    )
    client = ConnectClient()
    client._ensure_auth()
    record["environment_id"] = client._env_id
    from chalkcompute._gen.chalk.server.v1 import team_connect, team_pb

    environment = client.call(
        client.stub(team_connect.TeamServiceClientSync).get_env, team_pb.GetEnvRequest()
    ).environment
    parts = environment.kube_cluster_name.split("/")
    derived_context = "gke_" + parts[1] + "_" + parts[3] + "_" + parts[5]
    contexts = subprocess.run(
        ["kubectl", "config", "get-contexts", "-o", "name"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.splitlines()
    if derived_context not in contexts:
        raise RuntimeError("API-reported environment kube context is unavailable")
    guard.kube_context = derived_context
    guard.kube_contexts = [derived_context] + [
        value for value in contexts if "chalk-serverless-stage" in value
    ]
    record["kube_context"] = derived_context
    record["kube_contexts"] = guard.kube_contexts
    record["environment_name"] = environment.name
    record["expected_namespaces"] = [environment.id, environment.kube_job_namespace]
    guard.save()

    class BudgetClient(ConnectClient):
        def call(self, method, request, **kwargs):
            method_name = getattr(method, "__name__", "")
            if method_name in {"create_training_run", "create_scaling_group"}:
                # Check at the actual RPC, after any image build/source upload.
                guard.check_budget()
                record.setdefault("submission_inflight", {})[method_name] = True
                guard.save()
            response = super().call(method, request, **kwargs)
            if method_name in {"create_training_run", "create_scaling_group"}:
                record["submission_inflight"][method_name] = False
                guard.save()
            if method_name == "create_training_run" and response.training_run:
                guard.run_id = response.training_run.id
                record["training_run_id"] = guard.run_id
                guard.save()
                print(
                    json.dumps(
                        {
                            "phase": "training_created",
                            "run_id": guard.run_id,
                            "deadline_epoch": record.get("gpu_deadline_epoch"),
                        }
                    ),
                    flush=True,
                )
            if method_name == "get_training_run" and guard.run_id:
                resources = guard.capture_training_resource() or []
                if any(
                    resource.get("phase") in {"Failed", "Error"}
                    for resource in resources
                ):
                    raise RuntimeError("Training CR failed: " + repr(resources))
            if method_name == "create_scaling_group" and response.scaling_group:
                record.setdefault("group_ids", {})[response.scaling_group.name] = (
                    response.scaling_group.id
                )
                guard.save()
                print(
                    json.dumps(
                        {
                            "phase": "serving_group_created",
                            "name": response.scaling_group.name,
                            "group_id": response.scaling_group.id,
                            "deadline_epoch": record.get("gpu_deadline_epoch"),
                        }
                    ),
                    flush=True,
                )
            if method_name in {
                "create_training_run",
                "create_scaling_group",
            } and (
                (guard.out / "watchdog-expired").exists()
                or time.monotonic() >= guard.claim_deadline
            ):
                # A successful response may arrive after the deadline cleanup
                # began. Reconcile the now-known resource before unwinding.
                guard.cleanup_compute()
                raise TimeoutError("Submission completed after GPU deadline")
            return response

    budget_client = BudgetClient()
    guard.volume = cc.Volume(name)
    try:
        with tempfile.TemporaryDirectory(prefix="weight-training-overlay-") as staging:
            recipe = make_image(args.source_root, Path(staging))
            archive = Path(staging) / "weight-sdk-overlay.tar.gz"
            if args.prepared_image:
                prepared_archive = args.prepared_image.parent / archive.name
                if overlay_members(archive) != overlay_members(prepared_archive):
                    raise ValueError(
                        "Prepared image SDK source differs; rebuild before execution"
                    )
                archive = prepared_archive
                record["prepared_image_evidence"] = str(args.prepared_image)
            shutil.copyfile(archive, args.out / archive.name)
            record["source_overlay_sha256"] = hashlib.sha256(
                archive.read_bytes()
            ).hexdigest()
            guard.save()
            image = (
                cc.Image.from_json(args.prepared_image.read_text())
                if args.prepared_image
                else recipe.build(timeout=900)
            )
            (args.out / "image.json").write_text(image.to_json())
        dataset = DatasetClient().upload(name, FIXTURES)
        guard.dataset_id = dataset.dataset_id
        record["dataset_id"] = guard.dataset_id
        # Load the plain function module so the real decorator packages its source.
        module_path = Path(__file__).with_name("remote_training.py")
        source = module_path.read_bytes()
        (args.out / "remote-training-executed.py").write_bytes(source)
        record["remote_training_sha256"] = hashlib.sha256(source).hexdigest()
        record["harness_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        guard.save()
        spec = importlib.util.spec_from_file_location("remote_training", module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["remote_training"] = module
        spec.loader.exec_module(module)
        trainer = cc.training(
            dataset=name,
            image=image,
            name=name,
            gpu="nvidia-a100-80gb:1",
            cpu="16",
            memory="128Gi",
            compute_class=cc.ComputeClass.HOST,
            execution_target=cc.ExecutionBackend.SERVERLESS,
            env=remote_env,
            volumes=[
                cc.VolumeMount(name=name, mount_path="/chalk/weights", ref_name="main")
            ],
            max_retries=0,
        )(module.train_full_parameters)
        # Build the actual packaged training entrypoint before claiming A100s.
        # Reuse precisely this image spec when run_training submits the real RPC.
        from chalkcompute._connect import build_image

        deploy_image = trainer._build_deploy_image()
        record["training_deploy_image_uri"] = build_image(
            client, deploy_image, timeout=600
        )
        trainer._build_deploy_image = lambda: deploy_image
        guard.save()
        record["status"] = "cpu_prepared"
        guard.save()
        if not args.execute:
            return
        # Read-only preflight verifies CRD/RBAC access needed for real cleanup.
        guard.kube_items("chalkscalinggroups", name="sg-preflight-" + name)
        guard.kube_items("chalkcontainers", owner="preflight-" + name)
        if not guard.kube_items("namespaces", name=environment.kube_job_namespace):
            raise RuntimeError(
                "Authenticated environment namespace absent from cleanup contexts"
            )
        guard.volume.reload()
        guard.volume.put_file(
            "evidence/owned-seed-placeholder.txt",
            b"Unique smoke stream; seed publishes CURRENT atomically.\n",
        )
        if not args.seed_on_serving:
            # CPU-only model acquisition finishes before either A100 is claimed.
            seed = cc.Sandbox(
                name=name + "-seed",
                image=image,
                entrypoint=["sleep", "infinity"],
                cpu="2",
                memory="8Gi",
                compute_class=cc.ComputeClass.K8S,
                execution_target=cc.ExecutionBackend.SERVERLESS,
                lifetime="600s",
                env=remote_env,
            )
            guard.sandboxes.append(seed)
            seed.run(timeout=300)
            record["seed_sandbox_id"] = seed.id
            guard.save()
            execute(
                seed,
                "python3 -c "
                + shlex.quote(
                    f"from chalkcompute.weights import WeightStream; assert WeightStream({name!r},{model!r}).create().seed_from_hf({model!r},{revision!r}) == 0"
                ),
                timeout=480,
            )
            guard.stop_sandbox(seed)
            guard.sandboxes.remove(seed)
        # Detached process survives an abrupt launcher exit or SIGKILL.
        record["gpu_budget_started_at"] = time.time()
        record["gpu_deadline_epoch"] = (
            record["gpu_budget_started_at"] + args.budget_seconds
        )
        guard.claim_deadline = time.monotonic() + args.budget_seconds
        guard.save()
        watchdog_log = (args.out / "watchdog.log").open("ab", buffering=0)
        watchdog = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("resource_watchdog.py")),
                "--ledger",
                str(args.out / "launcher.json"),
                "--deadline",
                str(record["gpu_deadline_epoch"]),
                "--parent-pid",
                str(os.getpid()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=watchdog_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        record["watchdog_pid"] = watchdog.pid
        inhibitor = subprocess.Popen(
            ["caffeinate", "-i", "-w", str(watchdog.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        record["sleep_inhibitor_pid"] = inhibitor.pid
        guard.save()
        ready_deadline = time.monotonic() + 45
        while time.monotonic() < ready_deadline:
            if watchdog.poll() is not None:
                raise RuntimeError("Detached watchdog exited before readiness")
            if (args.out / "watchdog-ready").exists():
                break
            time.sleep(0.2)
        else:
            raise TimeoutError("Detached watchdog readiness was not confirmed")

        def serving_group(suffix, audit_version=None):
            guard.check_budget()
            group_env = {
                **remote_env,
                "CHALK_WEIGHT_SYNC_POLL_S": ".2",
                "CHALK_WEIGHT_STREAM_NAME": name,
                "CHALK_WEIGHT_MODEL": model,
            }
            if audit_version is not None:
                group_env["CHALK_WEIGHT_AUDIT_VERSION"] = str(audit_version)
            entrypoint = ["python3", "/test/serving_entrypoint.py"]
            if args.seed_on_serving and suffix == "-serve":
                entrypoint = [
                    "python3",
                    "-c",
                    f"from chalkcompute.weights import WeightStream; import runpy; assert WeightStream({name!r},{model!r}).create().seed_from_hf({model!r},{revision!r}) == 0; runpy.run_path('/test/serving_entrypoint.py',run_name='__main__')",
                ]
            group = cc.ScalingGroup(
                name=name + suffix,
                image=image,
                entrypoint=entrypoint,
                gpu="nvidia-a100-80gb:1",
                cpu="16",
                memory="128Gi",
                compute_class=cc.ComputeClass.HOST,
                execution_target=cc.ExecutionBackend.SERVERLESS,
                env=group_env,
                volumes=[
                    cc.VolumeMount(
                        name=name, mount_path="/chalk/weights", ref_name="main"
                    )
                ],
                port=8000,
                min_replicas=1,
                max_replicas=1,
                authenticated=True,
                readiness_probe=cc.ReadinessProbe.http("/chalk/health"),
            )
            guard.groups.append((name + suffix, group))
            group._client = budget_client
            group.deploy(ready_timeout=1200)
            record.setdefault("group_ids", {})[name + suffix] = group.id
            guard.save()
            return group

        serving = serving_group("-serve")
        record["baseline"] = sample(serving)
        record["client_disconnect"] = disconnect_probe(
            serving, guard, args.external_ssh_host
        )
        guard.save()
        config = {
            "stream": name,
            "model": model,
            "revision": revision,
            "steps": args.steps,
            "learning_rate": 0.05,
            "cpu_threads": args.cpu_threads,
        }
        guard.check_budget()
        handle = trainer.run_training(
            config=config, build_timeout=600, client=budget_client
        )
        guard.run_id = handle.run_id
        record["training_run_id"] = handle.run_id
        guard.save()
        observer = Observer(serving, args.out)
        observer.thread.start()
        try:
            handle.wait(
                timeout=args.budget_seconds
                - (time.time() - record["gpu_budget_started_at"])
            )
        finally:
            observer.close()
        report = json.loads(guard.volume.read_file("evidence/trainer.json"))
        (args.out / "trainer.json").write_text(json.dumps(report, indent=2))
        assert (
            report["status"] == "passed"
            and report["full_state_audit"]["all_tensor_bytes_equal"]
        )
        observed = {int(row["version"]) for row in observer.rows if "version" in row}
        published = {step["version"] for step in report["steps"]}
        record["observer_skipped_versions"] = sorted(published - observed)
        record["publish_to_first_response_s"] = {
            str(step["version"]): min(
                row["response_received_at"]
                for row in observer.rows
                if row.get("version") == str(step["version"])
            )
            - step["publish_completed_at"]
            for step in report["steps"]
            if step["version"] in observed
        }
        record["publish_to_response_timing_note"] = (
            "Cross-host wall-clock estimate; clock offset is unmeasured. Engine apply/pause metrics use local monotonic time."
        )
        guard.stop_training()
        version = report["full_state_audit"]["version"]
        # Verify visibility before a second engine load from a fresh independent reconstruction.
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            live = sample(serving)
            if live["version"] == str(version):
                break
            time.sleep(0.2)
        else:
            raise TimeoutError("Serving did not observe trained policy")
        record["live_trained"] = live
        final_identity = serving_replica_identities(serving, guard)
        final_processes = native_processes(final_identity[0][2], args.external_ssh_host)
        assert final_identity == [
            tuple(item) for item in record["client_disconnect"]["replicas_before"]
        ], "Serving replica restarted during publication"
        assert (
            final_processes == record["client_disconnect"]["native_processes_before"]
        ), "Native engine restarted during publication"
        record["in_place_identity"] = {
            "version": version,
            "replicas": final_identity,
            "native_processes": final_processes,
            "verified_at": time.time(),
        }
        guard.save()
        response = serving.call("/chalk/metrics", method="GET")
        (args.out / "trained-metrics.prom").write_bytes(response.content)
        guard.stop_group(name + "-serve", serving)
        guard.groups.remove((name + "-serve", serving))
        fresh = serving_group("-fresh", audit_version=version)
        record["fresh_trained"] = sample(fresh)
        assert record["live_trained"]["output"] == record["fresh_trained"]["output"]
        record["status"] = "passed"
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        cleanup_errors = []
        try:
            # Compute release precedes optional evidence I/O. The volume remains
            # available until the following data-cleanup phase.
            release_compute_then_retain(guard)
        except Exception as error:
            cleanup_errors.append(f"compute: {type(error).__name__}: {error}")
        finally:
            if cleanup_errors:
                record["cleanup_errors"] = cleanup_errors
                record["evidence_volumes_retained"] = True
                guard.save()
                raise RuntimeError(
                    "Compute cleanup incomplete; evidence volumes retained"
                )
            if any(record.get("submission_inflight", {}).values()):
                record["evidence_volumes_retained"] = True
                guard.save()
                raise RuntimeError(
                    "Ambiguous create RPC: detached watchdog remains armed"
                )
            cleanup_errors.extend(cleanup_data(guard))
            (args.out / "watchdog-disarmed").write_text(str(time.time()))
            record["finished_at"] = time.time()
            record["cleanup_errors"] = cleanup_errors
            guard.save()
            if cleanup_errors:
                raise RuntimeError("Cleanup incomplete; inspect launcher.json")


if __name__ == "__main__":

    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"Signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    main()
