"""An absent CR cannot hide a retained owner UID's orphan pod."""

import json
import time

import cpu_seed_status


def test_status_checks_retained_owner_pods_after_cr_disappears(tmp_path, monkeypatch):
    ledger = tmp_path / "launcher.json"
    record = {
        "name": "owned",
        "status": "released",
        "seed_sandbox_id": "sandbox",
        "cpu_deadline_epoch": time.time() + 60,
        "kube_contexts": ["exact-context"],
        "seed_resources": [
            {
                "context": "exact-context",
                "namespace": "exact-ns",
                "name": "cc-sandbox",
                "uid": "retained-uid",
                "phase": "Running",
            }
        ],
    }
    ledger.write_text(json.dumps(record))

    def kube(context, resource, selector):
        assert context == "exact-context"
        if resource == "chalkcontainers":
            return []
        assert resource == "pods" and selector == "metadata.namespace=exact-ns"
        return [
            {
                "metadata": {
                    "name": "orphan",
                    "uid": "pod-uid",
                    "ownerReferences": [{"uid": "retained-uid"}],
                },
                "status": {"phase": "Running"},
            }
        ]

    monkeypatch.setattr(cpu_seed_status, "kube", kube)
    result = cpu_seed_status.inspect(ledger)
    assert result["resources"][0]["cr_present"] is False
    assert result["resources"][0]["pods"][0]["name"] == "orphan"
    assert result["owned_cr_and_uid_pods_absent"] is False
