"""Profile-specific120-second CPU work reserve; immutable30-minute lease unchanged."""

from __future__ import annotations

import json
import os
import time

from profile_plan import write_json

RESERVE_SECONDS = 120


def work_remaining(record):
    seconds = record["cpu_deadline_epoch"] - time.time() - RESERVE_SECONDS
    if seconds <= 0:
        raise TimeoutError("CPU profile120-second cleanup reserve reached")
    return seconds


class ProfileSandboxStub:
    def __init__(self, original, record, ledger):
        self.original, self.record, self.ledger = original, record, ledger

    def __getattr__(self, name):
        return getattr(self.original, name)

    def create_sandbox(self, request, **kwargs):
        from cpu_seed import CREATE_RPC_SECONDS, CREATE_RECONCILE_GRACE_SECONDS

        ready = json.loads(
            (self.ledger.parent / "cpu-watchdog-ready.json").read_bytes()
        )
        if (
            ready.get("phase_id") != self.record["cpu_phase_id"]
            or ready.get("pid") != self.record["cpu_watchdog_pid"]
            or (self.ledger.parent / "cpu-expired").exists()
        ):
            raise RuntimeError("CPU profile create requires exact live ready guard")
        os.kill(ready["pid"], 0)
        seconds = min(CREATE_RPC_SECONDS, work_remaining(self.record))
        self.record.update(
            create_rpc_deadline_epoch=time.time() + seconds, submission_inflight=True
        )
        self.record["create_reconcile_until_epoch"] = (
            self.record["create_rpc_deadline_epoch"] + CREATE_RECONCILE_GRACE_SECONDS
        )
        write_json(self.ledger, self.record)
        response = self.original.create_once(
            request, timeout_ms=max(1, int(seconds * 1000)), **kwargs
        )
        if response.sandbox:
            self.record["seed_sandbox_id"] = response.sandbox.id
        self.record["submission_inflight"] = False
        write_json(self.ledger, self.record)
        work_remaining(self.record)
        return response


def guarded_client(guard, record, ledger):
    client = guard.guarded_sandbox_client(record, ledger)
    client._stub = ProfileSandboxStub(client._stub, record, ledger)
    return client


def poll(seed, record, ledger, code):
    while True:
        available = work_remaining(record)
        response = seed.exec(
            "python3", "-c", code, timeout_secs=max(1, min(20, int(available)))
        )
        if response.error is not None or response.exit_code not in (0, None):
            raise RuntimeError("Read-only CPU profile progress poll failed")
        progress = json.loads(response.stdout_text)
        write_json(ledger.parent / "seed-progress.json", progress)
        (ledger.parent / "seed-stdout.txt").write_text(progress["stdout"])
        (ledger.parent / "seed-stderr.txt").write_text(progress["stderr"])
        for field in ("started", "completed"):
            if (
                progress.get(field)
                and progress[field].get("phase_id") != record["cpu_phase_id"]
            ):
                raise RuntimeError("CPU profile progress phase differs")
        if progress.get("started"):
            record["seed_command_started_at"] = progress["started"]["started_at"]
        if progress.get("atomic_seed"):
            record.update(atomic_seed=progress["atomic_seed"], status="proving")
        record["last_progress_at"] = time.time()
        work_remaining(record)
        if progress.get("completed"):
            done = progress["completed"]
            record.update(
                seed_command_finished_at=done["finished_at"], guest_completion=done
            )
            write_json(ledger, record)
            if done["status"] != "passed":
                raise RuntimeError("CPU profile proof failed; inspect retained stderr")
            return progress
        write_json(ledger, record)
        time.sleep(min(5, work_remaining(record)))


def matching_crs(record, context, items):
    """Full frozen entrypoint hash proves phase; name/namespace labels alone don't."""
    import hashlib
    import re

    result = []
    for item in items:
        meta = item["metadata"]
        labels = meta.get("labels", {})
        if labels.get("chalk.ai/container-name") != record["seed_name"]:
            raise RuntimeError(
                "Exact-name recovery selector returned another container"
            )
        id = labels.get("chalk.ai/container", "")
        entrypoint = item.get("spec", {}).get("entrypoint")
        if (
            meta["namespace"] not in record["expected_namespaces"]
            or labels.get("chalk.ai/environment") != record["environment_id"]
            or not re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", id)
            or meta["name"] != "cc-" + id
        ):
            raise RuntimeError("Recovered CPU namespace/environment/UUID differs")
        if (
            not isinstance(entrypoint, list)
            or len(entrypoint) != 4
            or entrypoint[:3] != ["python3", "-u", "-c"]
            or hashlib.sha256(entrypoint[3].encode()).hexdigest()
            != record["guest_entrypoint_sha256"]
            or record["cpu_phase_id"] not in entrypoint[3]
            or item.get("spec", {}).get("image") != record["training_deploy_image_uri"]
        ):
            raise RuntimeError("Recovered CPU frozen entrypoint/phase differs")
        if record.get("seed_sandbox_id") not in (None, id):
            raise RuntimeError("Recovered CPU UUID changed")
        resource = {
            "context": context,
            "namespace": meta["namespace"],
            "name": meta["name"],
            "uid": meta["uid"],
        }
        for prior in record.get("seed_resources", []):
            if (
                prior["context"] == context
                and prior["name"] == meta["name"]
                and prior["uid"] != meta["uid"]
            ):
                raise RuntimeError("Recovered CPU owner UID changed")
        result.append((id, resource))
    return result


def recover_seed(record):
    """Bounded server-side exact-label lookup; no broad Sandbox.list_all fallback."""
    import subprocess
    import profile_host_guard as host

    deadline = time.monotonic() + 15
    matches = []
    contexts = list(dict.fromkeys([host.CONTEXT] + record["kube_contexts"]))
    for context in contexts:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Exact-name CPU recovery15-second budget exhausted")
        response = subprocess.run(
            [
                "kubectl",
                "--context",
                context,
                "get",
                "chalkcontainers",
                "-A",
                "-l",
                "chalk.ai/container-name=" + record["seed_name"],
                "-o",
                "json",
                "--request-timeout=" + str(max(1, int(min(5, remaining)))) + "s",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=min(6, remaining),
        )
        matches.extend(
            matching_crs(record, context, json.loads(response.stdout)["items"])
        )
    if len(matches) > 1:
        raise RuntimeError(
            "Exact CPU recovery found multiple phase/container identities"
        )
    if not matches:
        # Keep submission_inflight and its original reconcile deadline. A first
        # empty lookup cannot establish that a timed-out create never landed.
        return None
    id, resource = matches[0]
    record["seed_sandbox_id"] = id
    prior = {(v["context"], v["name"]): v for v in record.get("seed_resources", [])}
    prior[(resource["context"], resource["name"])] = resource
    record["seed_resources"] = list(prior.values())
    return id


def sandbox_rpc(sandbox_id, *, terminate=False, expected_name=None):
    """One exact15s RPC with no retry interceptor and no broad enumeration."""
    from chalkcompute import _sandbox as sm
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    client = sm.SandboxClient.from_env()
    try:
        stub = sm.pb_connect.SandboxServiceClientSync(
            client._address,
            protocol=sm.ProtocolType.GRPC,
            interceptors=(),
            http_client=sm.SYNC_HTTP_CLIENT,
            read_max_bytes=sm.RPC_READ_MAX_BYTES,
        )
        try:
            if terminate:
                stub.terminate_sandbox(
                    sm.pb.TerminateSandboxRequest(id=sandbox_id),
                    headers=client._headers,
                    timeout_ms=15000,
                )
                return {"id": sandbox_id, "status": "termination_requested"}
            response = stub.get_sandbox(
                sm.pb.GetSandboxRequest(id=sandbox_id),
                headers=client._headers,
                timeout_ms=15000,
            )
        except ConnectError as error:
            if error.code == Code.NOT_FOUND:
                return {"id": sandbox_id, "status": "not_found"}
            raise
        value = response.sandbox
        if (
            value is None
            or value.id != sandbox_id
            or (expected_name is not None and value.name != expected_name)
        ):
            raise RuntimeError("Exact CPU API identity/name differs")
        return {"id": sandbox_id, "status": value.status.name.casefold()}
    finally:
        client.close()


def release_cpu(guard, record, out, canonical_ledger=None):
    from profile_plan import write_json

    if not record.get("seed_sandbox_id"):
        recover_seed(record)
    id = record.get("seed_sandbox_id")
    if not id:
        if record.get("create_rpc_deadline_epoch") or record.get("submission_inflight"):
            raise RuntimeError(
                "CPU create remains ambiguous without exact recovered UUID"
            )
        record.update(cpu_compute_released=True, compute_never_submitted=True)
        return
    resource_guard = guard.ResourceGuard(
        record["name"],
        out,
        record,
        record["kube_context"],
        record["fallback_kube_context"],
    )
    resource_guard.capture_container_resource(id, "seed_resources")
    write_json(out / "launcher.json", record)
    if canonical_ledger is not None:
        from profile_host_guard import merge_retained_identity

        if canonical_ledger.exists():
            merge_retained_identity(record, json.loads(canonical_ledger.read_bytes()))
        write_json(canonical_ledger, record)
    sandbox_rpc(id, terminate=True)
    until = time.monotonic() + 120
    while True:
        status = sandbox_rpc(id, expected_name=record["seed_name"])["status"]
        if status in ("terminated", "not_found"):
            break
        if time.monotonic() >= until:
            raise RuntimeError("CPU API termination unconfirmed inside cleanup bound")
        time.sleep(min(5, max(0, until - time.monotonic())))
    resource_guard.verify_container_absent(id, "seed_resources")
    record.update(cpu_compute_released=True, cpu_compute_released_at=time.time())
    write_json(out / "launcher.json", record)


def install(guard, canonical_ledger=None):
    guard.recover_seed = recover_seed
    guard.release_cpu = lambda record, out: release_cpu(
        guard, record, out, canonical_ledger
    )
