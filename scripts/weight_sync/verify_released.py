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
    # Detached cleanup can recover a create after its response was lost. Merge
    # those immutable IDs before asking whether all owned resources are absent.
    for recovery in (
        "watchdog-cleanup/launcher.json",
        "placement-watchdog/launcher.json",
    ):
        path = args.ledger.parent / recovery
        if path.exists():
            recovered = json.loads(path.read_text())
            if recovered["name"] != record["name"]:
                raise RuntimeError("Cleanup recovery ledger belongs to another run")
            record.setdefault("group_ids", {}).update(recovered.get("group_ids", {}))
            record.setdefault("group_resources", {}).update(
                recovered.get("group_resources", {})
            )
            if recovered.get("training_run_id"):
                record["training_run_id"] = recovered["training_run_id"]
            if recovered.get("training_resources"):
                record["training_resources"] = recovered["training_resources"]
    guard.run_id = record.get("training_run_id")
    for name, resource in record.get("group_resources", {}).items():
        record.setdefault("group_ids", {})[name] = resource["id"]
        if name not in owners:
            if len(resource["owners"]) != 1:
                raise RuntimeError("Scaling group has ambiguous retained owner UIDs")
            owners[name] = {
                "uid": resource["owners"][0],
                "context": resource["context"],
            }
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
    from chalkcompute._gen.chalk.server.v1 import training_runs_pb as tp
    from chalkcompute._gen.chalk.container.v1 import (
        service_connect as cs,
        service_pb as cp,
    )

    runs = guard.recover_run(client)
    training = []
    if guard.run_id and not any(run.id == guard.run_id for run in runs):
        raise RuntimeError(
            "Known training run absent from exact-name list; cannot prove release"
        )
    resources = record.get("training_resources", [])
    if runs and not resources:
        raise RuntimeError(
            "Training owner UID evidence missing; cannot verify owned pod absence"
        )
    for run in runs:
        if run.status not in {
            tp.TrainingRunStatus.COMPLETED,
            tp.TrainingRunStatus.FAILED,
            tp.TrainingRunStatus.CANCELED,
        }:
            raise RuntimeError("Owned training run is still active")
        for context in guard.kube_contexts:
            assert not guard.kube_items(
                "chalkcontainers", name="cc-" + run.id, context=context
            ), "Training CR still exists"
        for resource in resources:
            pods = guard.kube_items("pods", context=resource["context"])
            assert not any(
                any(
                    owner.get("uid") == resource["uid"]
                    for owner in pod["metadata"].get("ownerReferences", [])
                )
                for pod in pods
            ), "Owned training pod remains"
        container = client.call(
            client.stub(cs.ContainerServiceClientSync).get_container,
            cp.GetContainerRequest(id=run.id, include_stopped=True),
        ).container
        if container is not None and container.status.lower() not in {
            "succeeded",
            "failed",
            "terminated",
            "error",
        }:
            raise RuntimeError("Training container API is not terminal")
        training.append(
            {
                "id": run.id,
                "status": run.status.name,
                "cr_absent": True,
                "owned_pods_absent": True,
                "container_terminal_or_absent": True,
            }
        )
    proof["training_runs"] = training
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
        if name == guard.name and not record.get("owns_source_volume", True):
            continue
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
    owned_ids = list(record.get("group_ids", {}).values()) + [run.id for run in runs]
    owned = [
        item
        for item in json.loads(result.stdout)
        if any(owned_id in item.get("id", "") for owned_id in owned_ids)
    ]
    assert not owned, "Owned host runtime still exists"
    proof["owned_host_runtimes"] = owned
    path = args.ledger.parent / "cleanup-proof.json"
    path.write_text(json.dumps(proof, indent=2))
    print(json.dumps({"proof": str(path), "all_owned_resources_absent": True}))


if __name__ == "__main__":
    main()
