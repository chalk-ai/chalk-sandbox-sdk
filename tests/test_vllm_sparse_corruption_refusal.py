"""Real SDK checksum rejection plus exact pinned executor queue regression."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch

from chalkcompute._weights_codec import TensorPatch
from chalkcompute._weights_format import WeightStreamError
from scripts.vllm_sparse import audit_extension as audit
from scripts.vllm_sparse import pod_script as pod
from scripts.vllm_sparse import proof, queue_regression

TRANSACTION = "a" * 32


@pytest.fixture
def refusal_case(tmp_path, monkeypatch):
    directory = tmp_path / "corrupt"
    patches = [
        TensorPatch(
            name,
            shape,
            "bfloat16",
            torch.ones(len(indices), dtype=torch.bfloat16),
            torch.tensor(indices),
        )
        for name, shape, indices in [
            (proof.NORM, (7168,), proof.target_indices()[proof.NORM]),
            (proof.HEAD, (163840, 7168), proof.target_indices()[proof.HEAD]),
        ]
    ]
    proof.write_sparse_version(directory, 4, 3, patches, "b" * 64)
    payload = directory / "patch-0.bin"
    raw = bytearray(payload.read_bytes())
    raw[-1] ^= 1
    payload.write_bytes(raw)
    baseline, workers = [], []
    monkeypatch.setattr(
        audit.WeightSyncExtension,
        "ws_sparse_probe",
        lambda worker: copy.deepcopy(worker.fixture_probe),
        raising=False,
    )
    for rank in range(8):
        targets = {
            name: {
                "storage_ptr": 1000 + rank * 10 + offset,
                "data_ptr": 1000 + rank * 10 + offset,
                "shape": shape,
            }
            for offset, (name, shape) in enumerate(
                [(proof.NORM, [7168]), (proof.HEAD, [20480, 7168])]
            )
        }
        before = {
            "rank": rank,
            "world_size": 8,
            "pid": 100 + rank,
            "process_start_ticks": str(10000 + rank),
            "device": f"cuda:{rank}",
            "model_id": 100000 + rank,
            "tensors": targets,
        }
        baseline.append(before)
        worker = audit.SparseAuditExtension()
        worker.fixture_model = torch.nn.Linear(2, 2, bias=False)
        worker.model_runner = SimpleNamespace(
            get_model=lambda worker=worker: worker.fixture_model
        )
        worker.fixture_probe = {
            "profile": "kimi-k3-bf16-tp8-v1",
            "rank": rank,
            "tp_rank": rank,
            "tp_world_size": 8,
            "pid": before["pid"],
            "pid_start_ticks": int(before["process_start_ticks"]),
            "device": before["device"],
            "model_id": before["model_id"],
            "version": 3,
            "identity_sha256": f"identity-{rank}",
            "targets": targets,
        }
        workers.append(worker)
    return directory, baseline, workers


def test_actual_sdk_checksum_refusal_returns_eight_builtin_nonmutating_receipts(
    refusal_case,
):
    directory, baseline, workers = refusal_case
    before = [worker.fixture_model.weight.detach().clone() for worker in workers]
    receipts = [
        worker.ws_audit_corrupt_refusal(str(directory), 4, TRANSACTION)
        for worker in workers
    ]
    evidence = proof.corrupt_payload_evidence(directory)
    proof.validate_refusal_records(receipts, baseline, TRANSACTION, evidence)
    assert evidence["actual_checksum"] != evidence["expected_checksum"]
    for worker, original, receipt in zip(workers, before, receipts, strict=True):
        assert torch.equal(worker.fixture_model.weight, original)
        assert receipt["error"] == "Checksum mismatch for " + proof.NORM
        assert receipt["sparse_writes"] == []
        assert getattr(worker, "_ws_resident_pending", None) is None
        audit.rpc_metadata(receipt)


def test_corrupt_prepare_acceptance_is_unconditionally_a_failure(
    refusal_case, monkeypatch
):
    directory, _, workers = refusal_case
    monkeypatch.setattr(
        workers[0], "ws_audit_prepare_dir", lambda *_: {"prepared": True}
    )
    with pytest.raises(RuntimeError, match="unexpectedly prepared"):
        workers[0].ws_audit_corrupt_refusal(str(directory), 4, TRANSACTION)


@pytest.mark.parametrize("kind", ["unknown", "wrong_message", "subclass"])
def test_only_exact_sdk_error_type_and_expected_checksum_message_are_receipts(
    refusal_case, monkeypatch, kind
):
    directory, _, workers = refusal_case

    class OtherChecksumError(WeightStreamError):
        pass

    original = {
        "unknown": RuntimeError("unknown failure"),
        "wrong_message": WeightStreamError("Checksum mismatch for " + proof.HEAD),
        "subclass": OtherChecksumError("Checksum mismatch for " + proof.NORM),
    }[kind]

    def fail(*_):
        raise original

    monkeypatch.setattr(workers[0], "ws_audit_prepare_dir", fail)
    with pytest.raises(type(original)) as error:
        workers[0].ws_audit_corrupt_refusal(str(directory), 4, TRANSACTION)
    assert error.value is original


@pytest.mark.parametrize("mutation", ["pending", "version", "write", "payload"])
def test_expected_error_does_not_excuse_state_write_or_payload_mutation(
    refusal_case, monkeypatch, mutation
):
    directory, _, workers = refusal_case
    worker = workers[0]

    def fail(*_):
        if mutation == "pending":
            worker._ws_resident_pending = {"transaction_id": TRANSACTION}
        elif mutation == "version":
            worker.fixture_probe["version"] = 4
        elif mutation == "write":
            with torch.no_grad():
                worker.fixture_model.weight.view(-1).index_copy_(
                    0, torch.tensor([0]), torch.tensor([1.0])
                )
        else:
            payload = directory / "patch-0.bin"
            payload.write_bytes(payload.read_bytes()[:-1] + b"x")
        raise WeightStreamError("Checksum mismatch for " + proof.NORM)

    monkeypatch.setattr(worker, "ws_audit_prepare_dir", fail)
    with pytest.raises(RuntimeError, match="changed resident state or payload"):
        worker.ws_audit_corrupt_refusal(str(directory), 4, TRANSACTION)


@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "duplicate",
        "transaction",
        "checksum",
        "pending",
        "writes",
        "version",
        "identity",
    ],
)
def test_host_rejects_missing_duplicate_or_wrong_refusal_receipts(
    refusal_case, invalid
):
    directory, baseline, workers = refusal_case
    receipts = [
        worker.ws_audit_corrupt_refusal(str(directory), 4, TRANSACTION)
        for worker in workers
    ]
    if invalid == "missing":
        receipts.pop()
    elif invalid == "duplicate":
        receipts[-1] = receipts[0]
    else:
        key, value = {
            "transaction": ("transaction_id", "wrong"),
            "checksum": ("actual_checksum", "wrong"),
            "pending": ("pending_after", True),
            "writes": ("sparse_writes", [{"op": "index_copy_"}]),
            "version": ("version_after", 4),
            "identity": ("pid_start_ticks", 999),
        }[invalid]
        receipts[0][key] = value
    with pytest.raises(ValueError):
        proof.validate_refusal_records(
            receipts, baseline, TRANSACTION, proof.corrupt_payload_evidence(directory)
        )


def test_host_consumes_all_receipts_then_requires_normal_abort(refusal_case, tmp_path):
    directory, baseline, workers = refusal_case
    receipts = [
        worker.ws_audit_corrupt_refusal(str(directory), 4, TRANSACTION)
        for worker in workers
    ]
    events = []

    def rpc(method, args):
        events.append(method)
        if method == "ws_audit_corrupt_refusal":
            return receipts
        return [{"ok": True, "aborted": True, "transaction_id": TRANSACTION}] * 8

    experiment = SimpleNamespace(rpc=rpc, baseline=baseline, directory=tmp_path)
    pod.corrupt_refusal(experiment, directory, TRANSACTION)
    assert events == ["ws_audit_corrupt_refusal", "ws_sparse_abort"]
    assert (
        json.loads((tmp_path / "checksum-refusal.json").read_text())["passed"] is True
    )


def test_exact_pinned_executor_drains_explicit_receipts_but_not_worker_exceptions():
    source = Path(__file__).parent / "fixtures/vllm_v0330_multiproc_executor.txt"
    call = queue_regression.extract(source)
    failures = queue_regression.scenario(call, explicit_receipts=False)
    assert failures[0]["queued_per_rank"] == [0, 1, 1, 1, 1, 1, 1, 1]
    assert [result["success"] for result in failures] == [False, False]
    successes = queue_regression.scenario(call, explicit_receipts=True)
    assert all(
        result["success"]
        and result["response_count"] == 8
        and result["queued_per_rank"] == [0] * 8
        for result in successes
    )
