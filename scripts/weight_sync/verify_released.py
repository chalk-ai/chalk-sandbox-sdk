"""Read-only absence proof for one launcher ledger, including retained owner UIDs."""

import argparse
import json
import shlex
import subprocess
import time
from pathlib import Path

from ftqa_smoke import load_env
from training_smoke import ResourceGuard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--ssh-host", required=True)
    args = parser.parse_args()
    load_env(args.env_file)
    import chalkcompute as cc
    from chalkcompute._connect import ConnectClient
    from chalkcompute._gen.chalk.server.v1 import (
        datasets_connect as dc,
        datasets_pb as dp,
    )
    from chalkcompute._volume import VolumeNotFoundError
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    record = json.loads(args.ledger.read_text())
    guard = ResourceGuard(
        record["name"], args.ledger.parent, record, args.context, args.context
    )
    client = ConnectClient()
    client._ensure_auth()
    assert client._env_id == record["environment_id"]
    proof = {"checked_at": time.time(), "name": guard.name, "cleanup_errors": []}
    owners_path = args.ledger.parent / "placement-watchdog" / "owners.json"
    owners = json.loads(owners_path.read_text()) if owners_path.exists() else {}
    groups = []
    for name, group_id in record.get("group_ids", {}).items():
        owner = owners[name]
        crs = guard.kube_items(
            "chalkscalinggroups", name="sg-" + group_id, context=owner["context"]
        )
        children = guard.kube_items(
            "chalkcontainers", owner=owner["uid"], context=owner["context"]
        )
        pods = guard.kube_items("pods", owner=owner["uid"], context=owner["context"])
        assert not crs and not children and not pods, "Owned compute still exists"
        try:
            cc.ScalingGroup.from_name(name)
        except Exception as error:
            cause = error if isinstance(error, ConnectError) else error.__cause__
            assert isinstance(cause, ConnectError) and cause.code == Code.NOT_FOUND, (
                type(error).__name__
            )
        else:
            raise RuntimeError("ScalingGroup API association still exists")
        groups.append(
            {
                "id": group_id,
                "owner_uid": owner["uid"],
                "context": owner["context"],
                "cr_absent": True,
                "owned_containers_absent": True,
                "owned_pods_absent": True,
                "api_absent": True,
            }
        )
    proof["groups"] = groups
    assert not guard.recover_run(client), (
        "Training run was submitted; inspect its exact CR before release proof"
    )
    proof["training_runs"] = []
    if guard.dataset_id:
        stub = client.stub(dc.DatasetMetadataServiceClientSync)
        response = client.call(
            stub.list_datasets,
            dp.ListDatasetsRequest(
                filters=dp.ListDatasetsFilters(ids=[guard.dataset_id])
            ),
        )
        assert not response.datasets
        proof["dataset"] = {"id": guard.dataset_id, "active_catalog_absent": True}
    volumes = []
    for name in [
        guard.name,
        "training-src-" + guard.name,
        "training-data-" + guard.name,
        "training-ckpt-" + guard.name,
    ]:
        try:
            cc.Volume(name, create_if_missing=False).reload()
        except VolumeNotFoundError:
            volumes.append({"name": name, "absent": True})
        else:
            raise RuntimeError("Owned volume still exists: " + name)
    proof["volumes"] = volumes
    command = [
        "sudo",
        "docker",
        "exec",
        "chalk-external-hypervisor",
        "runsc",
        "--root=/var/lib/chalk-hypervisor/runsc",
        "list",
        "--format=json",
    ]
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            args.ssh_host,
            shlex.join(command),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    group_ids = record.get("group_ids", {}).values()
    owned = [
        item
        for item in json.loads(result.stdout)
        if any(group_id in item.get("id", "") for group_id in group_ids)
    ]
    assert not owned, "Owned host runtime still exists"
    proof["owned_host_runtimes"] = owned
    path = args.ledger.parent / "cleanup-proof.json"
    path.write_text(json.dumps(proof, indent=2))
    print(json.dumps({"proof": str(path), "all_owned_resources_absent": True}))


if __name__ == "__main__":
    main()
