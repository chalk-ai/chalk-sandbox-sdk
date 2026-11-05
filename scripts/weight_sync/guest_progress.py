"""Inspect only an owned live-smoke guest through its known external host."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from pathlib import Path

from training_smoke import ResourceGuard

GUEST_CODE = """
import json
from pathlib import Path
root = Path('/chalk/weights')
cache = Path('/root/.cache/huggingface/hub')
command = Path('/proc/1/cmdline').read_bytes()
print(json.dumps({
    'stage': 'serving' if b'chalkcompute.weights.serving' in command else ('training' if b'_training_entrypoint' in command else 'seed_or_audit'),
    'pid1': {line.split(':', 1)[0]: line.split(':', 1)[1].strip() for line in Path('/proc/1/status').read_text().splitlines() if line.startswith(('State:', 'VmRSS:', 'Threads:'))},
    'cached_checkpoint_bytes': sum(p.stat().st_size for p in cache.glob('models--Qwen--Qwen3-8B/blobs/*') if p.is_file()),
    'weight_files': [(str(p.relative_to(root)), p.stat().st_size) for p in root.rglob('*') if p.is_file() and (p.suffix == '.safetensors' or p.name == 'CURRENT')][:12],
}))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--group-name")
    parser.add_argument("--replica")
    parser.add_argument("--training", action="store_true")
    args = parser.parse_args()
    record = json.loads(args.ledger.read_text())
    guard = ResourceGuard(
        record["name"], args.ledger.parent, record, args.context, args.context
    )
    if args.training:
        args.replica = "cc-" + record["training_run_id"]
        resources = guard.kube_items("chalkcontainers", name=args.replica)
        assert len(resources) == 1
        metadata = resources[0]["metadata"]
        assert any(
            owner["uid"] == metadata["uid"]
            for owner in record.get("training_resources", [])
        ), "Training CR UID was not captured by this test"
    else:
        assert args.group_name and args.replica, (
            "Group name and exact replica are required"
        )
        group_id = record["group_ids"][args.group_name]
        groups = guard.kube_items("chalkscalinggroups", name="sg-" + group_id)
        assert len(groups) == 1
        metadata = groups[0]["metadata"]
        children = guard.kube_items("chalkcontainers", owner=metadata["uid"])
        assert any(item["metadata"]["name"] == args.replica for item in children), (
            "Replica is not owned by the recorded test group"
        )
    assert metadata["namespace"] in record["expected_namespaces"]
    command = [
        "sudo",
        "docker",
        "exec",
        "chalk-external-hypervisor",
        "runsc",
        "--root=/var/lib/chalk-hypervisor/runsc",
        "exec",
        args.replica,
        "python3",
        "-c",
        GUEST_CODE,
    ]
    subprocess.run(
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
        timeout=45,
    )


if __name__ == "__main__":
    main()
