"""Per-name metadata create ownership and post-writer late-create reconciliation."""

from __future__ import annotations

import json
import os
import time

from cpu_seed import remaining, write_json

RPC_SECONDS = 30
RECONCILE_SECONDS = 90


def marker_path(out, name):
    if not name.startswith("weight-profile-") or any(
        c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name
    ):
        raise ValueError("Require the exact owned profiling volume name")
    return out / ("volume-create-" + name + ".json")


def require_cpu_guard(record, out):
    if remaining(record) < 120 + RPC_SECONDS:
        raise TimeoutError("CPU cleanup reserve reached")
    ready = json.loads((out / "cpu-watchdog-ready.json").read_bytes())
    if (
        ready.get("phase_id") != record["cpu_phase_id"]
        or ready.get("pid") != record["cpu_watchdog_pid"]
        or (out / "cpu-expired").exists()
    ):
        raise RuntimeError("CPU metadata create requires exact live ready guard")
    os.kill(ready["pid"], 0)


def create_owned(record, out, name, create_call):
    require_cpu_guard(record, out)
    if name not in record["owned_volume_names"]:
        raise ValueError("Volume name is not owned by this phase")
    path = marker_path(out, name)
    if path.exists():
        raise RuntimeError("Metadata create already attempted; replay refused")
    now = time.time()
    marker = {
        "phase_id": record["cpu_phase_id"],
        "name": name,
        "status": "inflight",
        "started_at": now,
        "rpc_deadline_epoch": now + RPC_SECONDS,
        "reconcile_until_epoch": now + RECONCILE_SECONDS,
    }
    write_json(path, marker)
    result = create_call(RPC_SECONDS)
    if result.volume is None:
        raise RuntimeError("Volume create lacked acknowledgement")
    marker.update(status="acknowledged", acknowledged_at=time.time())
    write_json(path, marker)
    record.setdefault("volume_creation_markers", {})[name] = marker
    write_json(out / "launcher.json", record)
    return result


def create_volumes(record, out):
    import chalkcompute as cc
    from chalkcompute._connect import (
        ConnectClient,
        ProtocolType,
        SYNC_HTTP_CLIENT,
        RPC_READ_MAX_BYTES,
    )
    from chalkcompute._gen.chalk.volume.v2 import volume_connect, volume_pb
    from chalkcompute._volume import VolumeNotFoundError
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    client = ConnectClient()
    client._ensure_auth()
    if client._env_id != record["environment_id"]:
        raise RuntimeError("Volume create environment changed")
    assert client._api_server is not None
    stub = volume_connect.VolumeServiceClientSync(
        client._api_server,
        protocol=ProtocolType.GRPC,
        interceptors=(),
        http_client=SYNC_HTTP_CLIENT,
        read_max_bytes=RPC_READ_MAX_BYTES,
    )
    client._typed_clients[volume_connect.VolumeServiceClientSync] = stub
    volumes = cc.VolumeClient.from_connect(client)
    for name in record["owned_volume_names"]:
        require_cpu_guard(record, out)
        try:
            cc.Volume(name, client=volumes, create_if_missing=False).reload()
        except VolumeNotFoundError:
            pass
        else:
            raise RuntimeError("Planned volume pre-exists; ownership is not ours")
        try:
            create_owned(
                record,
                out,
                name,
                lambda timeout: client.call(
                    stub.create_volume,
                    volume_pb.CreateVolumeRequest(name=name),
                    timeout=timeout,
                ),
            )
        except ConnectError as error:
            if error.code == Code.ALREADY_EXISTS:
                marker = json.loads(marker_path(out, name).read_bytes())
                marker["status"] = "collision"
                write_json(marker_path(out, name), marker)
            raise


def release_created(record, out, release, save=write_json):
    """Compute/writer must already be stopped; first NotFound is insufficient."""
    if (
        record.get("cpu_compute_released") is not True
        or record.get("host_runtime_verified_absent") is not True
    ):
        raise RuntimeError("Stop/prove CPU writer and HOST runtime before data release")
    planned = list(record["owned_volume_names"])
    while True:
        owned = []
        until = 0
        for name in planned:
            path = marker_path(out, name)
            if not path.exists():
                continue  # Never delete a planned-name collision.
            marker = json.loads(path.read_bytes())
            if (
                marker.get("phase_id") != record["cpu_phase_id"]
                or marker.get("name") != name
            ):
                raise RuntimeError("Volume create ownership marker differs")
            if (
                marker["rpc_deadline_epoch"] != marker["started_at"] + RPC_SECONDS
                or marker["reconcile_until_epoch"]
                != marker["started_at"] + RECONCILE_SECONDS
            ):
                raise RuntimeError("Volume create deadline/reconciliation changed")
            if marker["status"] == "collision":
                continue
            if marker["status"] not in ("inflight", "acknowledged"):
                raise RuntimeError("Unknown metadata create state")
            owned.append(name)
            if marker["status"] == "inflight":
                until = max(until, marker["reconcile_until_epoch"])
        record["owned_volume_names"] = owned
        try:
            release(record)
        finally:
            record["owned_volume_names"] = planned
        if time.time() >= until:
            record["volume_creates_reconciled_at"] = time.time()
            record["planned_volume_names"] = planned
            record["owned_volume_names"] = owned
            return
        record["volume_verified_absent"] = False
        save(
            out / "volume-reconciliation-progress.json",
            {
                "phase_id": record["cpu_phase_id"],
                "until": until,
                "checked_at": time.time(),
            },
        )
        time.sleep(max(0, min(2, until - time.time())))


def install_cpu_cleanup(guard, out):
    original = guard.release_data
    guard.release_data = lambda record: release_created(
        record, out, original, guard.write_json
    )
