"""Synthetic exported receipts exercise the offline verifier, without a GPU."""

import base64
import copy
import hashlib
import json
import subprocess
import sys

import pytest

from scripts.vllm_sparse import summarize_result as summary


def write(path, value):
    path.write_text(json.dumps(value))


def mutate(path, update):
    value = json.loads(path.read_text())
    update(value)
    write(path, value)


@pytest.fixture
def exported(tmp_path):
    """Tiny BF16 norm bytes and head samples; never materialize the large head."""
    baseline = []
    raw = b"\x80\x3f" * summary.WIDTH
    for rank in range(8):
        tensors = {}
        for offset, (name, shape) in enumerate(
            [(summary.NORM, [7168]), (summary.HEAD, [20480, 7168])]
        ):
            tensors[name] = {
                "shape": shape,
                "dtype": "bfloat16",
                "storage_ptr": 1000 + rank * 10 + offset,
                "data_ptr": 1000 + rank * 10 + offset,
            }
        tensors[summary.NORM].update(
            base64=base64.b64encode(raw).decode(),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
        tensors[summary.HEAD].update(
            omitted_indices=summary.HEAD_INDICES,
            selected_base64={
                str(index): base64.b64encode(b"\x80\x3f").decode()
                for index in summary.HEAD_INDICES
            },
            masked_sha256=hashlib.sha256(
                f"synthetic head rank {rank}".encode()
            ).hexdigest(),
        )
        baseline.append(
            {
                "rank": rank,
                "world_size": 8,
                "pid": 100 + rank,
                "process_start_ticks": str(10000 + rank),
                "device": f"cuda:{rank}",
                "device_uuid": f"synthetic-{rank}",
                "model_id": 100000 + rank,
                "tensors": tensors,
            }
        )
    write(tmp_path / "scope.json", {"status": "passed", "stream_drain": "not_proven"})
    write(tmp_path / "A.json", baseline)
    write(tmp_path / "A-unchanged-repeat.json", baseline)
    for label, xor in [
        ("B-after", 1),
        ("B-replay", 1),
        ("C-after", 3),
        ("C-replay", 3),
        ("inverse_A-after", 0),
        ("inverse_A-replay", 0),
        ("corruption-refusal", 0),
    ]:
        records = copy.deepcopy(baseline)
        for record in records:
            norm = record["tensors"][summary.NORM]
            observed = bytearray(raw)
            for index in summary.NORM_INDICES:
                observed[index * 2] ^= xor
            norm.update(
                base64=base64.b64encode(observed).decode(),
                sha256=hashlib.sha256(observed).hexdigest(),
            )
            head = record["tensors"][summary.HEAD]
            head["selected_base64"] = {
                key: base64.b64encode(bytes([0x80 ^ xor, 0x3F])).decode()
                for key in head["selected_base64"]
            }
        write(tmp_path / f"{label}.json", records)
    write(
        tmp_path / "PREUPDATE-criterion.json",
        {"frozen_before_updates": True, "logprob_tolerance": 0.001},
    )
    for call in range(6):
        version, replay = call // 2 + 1, bool(call % 2)
        identities = {str(rank): f"identity-{rank}" for rank in range(8)}
        workers = []
        for rank in range(8):
            workers.append(
                {
                    "rank": rank,
                    "pid": baseline[rank]["pid"],
                    "result": {
                        "committed": True,
                        "ok": True,
                        "prepared": False,
                        "transaction_id": f"transaction-{version}",
                        "version": version,
                        "parent_version": version - 1,
                        "manifest_sha256": f"manifest-{version}",
                        "identity_sha256": identities[str(rank)],
                    },
                    "full_loaders_forbidden": True,
                    "idempotent_no_write_replay": replay,
                    "commit_and_sync_seconds": 0.01,
                    "maximum_new_storage_bytes": 16,
                    "sparse_writes": []
                    if replay
                    else [
                        {
                            "storage_ptr": tensor["storage_ptr"],
                            "op": "aten.index_copy_.default",
                        }
                        for tensor in baseline[rank]["tensors"].values()
                    ],
                }
            )
        write(
            tmp_path / f"rpc-{call:03d}.json",
            {
                "request": {
                    "method": "ws_audit_commit",
                    "args": [
                        f"transaction-{version}",
                        version,
                        identities,
                        f"manifest-{version}",
                    ],
                },
                "response": {"results": workers},
                "started": 100.0 + call,
                "finished": 100.2 + call,
            },
        )
    write(
        tmp_path / "rpc-007-error.json",
        {
            "error": "HTTPError",
            "request": {
                "method": "ws_audit_prepare_dir",
                "args": ["corrupt", 4, "bad"],
            },
            "body": "Checksum mismatch for " + summary.NORM,
        },
    )
    return tmp_path


def test_valid_export_checks_bytes_controls_and_retains_scope_limits(exported):
    result = summary.verify_sparse(exported)
    assert result["passed"] is True
    assert result["rank_count"] == 8
    assert len(result["snapshot_checks"]) == 7
    assert [(v["version"], v["replay"]) for v in result["commit_calls"]] == [
        (1, False),
        (1, True),
        (2, False),
        (2, True),
        (3, False),
        (3, True),
    ]
    assert result["scope_record"]["stream_drain"] == "not_proven"
    assert "receipts only" in result["verification_scope"]


@pytest.mark.parametrize(
    "label", ["B-after", "C-replay", "inverse_A-after", "corruption-refusal"]
)
def test_reject_changed_or_unchanged_norm_byte_corruption(exported, label):
    def corrupt(records):
        norm = records[0]["tensors"][summary.NORM]
        raw = bytearray(base64.b64decode(norm["base64"]))
        raw[12] ^= 1  # Outside the sparse coordinate set, with a matching raw hash.
        norm["base64"] = base64.b64encode(raw).decode()
        norm["sha256"] = hashlib.sha256(raw).hexdigest()

    mutate(exported / f"{label}.json", corrupt)
    with pytest.raises(ValueError, match="unchanged bytes"):
        summary.verify_sparse(exported)


@pytest.mark.parametrize("key", ["selected_base64", "masked_sha256", "omitted_indices"])
def test_reject_head_sample_or_unchanged_digest_conflicts(exported, key):
    def corrupt(records):
        head = records[3]["tensors"][summary.HEAD]
        if key == "selected_base64":
            head[key]["0"] = base64.b64encode(b"\x80\x3f").decode()
        elif key == "omitted_indices":
            head[key] = [0, 1, 2, 3]
        else:
            head[key] = "0" * 64

    mutate(exported / "B-after.json", corrupt)
    with pytest.raises(ValueError):
        summary.verify_sparse(exported)


@pytest.mark.parametrize("key", ["pid", "process_start_ticks", "device", "model_id"])
def test_reject_worker_replacement(exported, key):
    mutate(exported / "C-after.json", lambda rows: rows[0].update({key: "replacement"}))
    with pytest.raises(ValueError):
        summary.verify_sparse(exported)


@pytest.mark.parametrize(
    ("call", "key", "value"),
    [
        (0, "pid", 999),
        (0, "result", {"committed": False}),
        (0, "full_loaders_forbidden", False),
        (0, "maximum_new_storage_bytes", 4097),
        (0, "maximum_new_storage_bytes", float("nan")),
        (0, "commit_and_sync_seconds", float("nan")),
        (1, "idempotent_no_write_replay", False),
        (1, "sparse_writes", [{"op": "aten.index_copy_", "storage_ptr": 1000}]),
        (0, "sparse_writes", [{"op": "aten.copy_", "storage_ptr": 123}]),
    ],
)
def test_reject_unproven_commit_or_replay(exported, call, key, value):
    mutate(
        exported / f"rpc-{call:03d}.json",
        lambda rpc: rpc["response"]["results"][0].update({key: value}),
    )
    with pytest.raises(ValueError):
        summary.verify_sparse(exported)


def test_reject_replay_transaction_change(exported):
    mutate(
        exported / "rpc-001.json",
        lambda rpc: rpc["request"]["args"].__setitem__(0, "other"),
    )
    with pytest.raises(ValueError, match="transaction identity"):
        summary.verify_sparse(exported)


@pytest.mark.parametrize(
    "key", ["version", "transaction_id", "manifest_sha256", "identity_sha256"]
)
def test_commit_ack_must_match_requested_version_and_identities(exported, key):
    mutate(
        exported / "rpc-000.json",
        lambda rpc: rpc["response"]["results"][0]["result"].update({key: "conflict"}),
    )
    with pytest.raises(ValueError, match="acknowledgement"):
        summary.verify_sparse(exported)


def test_commit_requires_complete_distinct_rank_identity_map(exported):
    mutate(
        exported / "rpc-000.json", lambda rpc: rpc["request"]["args"].__setitem__(2, {})
    )
    with pytest.raises(ValueError, match="rank identities"):
        summary.verify_sparse(exported)


def test_reject_missing_rank_and_missing_rpc(exported):
    mutate(exported / "rpc-000.json", lambda rpc: rpc["response"]["results"].pop())
    with pytest.raises(ValueError, match="rank coverage"):
        summary.verify_sparse(exported)
    (exported / "rpc-000.json").unlink()
    with pytest.raises(ValueError):
        summary.verify_sparse(exported)


def test_reject_loosened_criterion_and_generic_corruption_error(exported):
    criterion = exported / "PREUPDATE-criterion.json"
    mutate(criterion, lambda v: v.update(logprob_tolerance=0.01))
    with pytest.raises(ValueError, match="criterion"):
        summary.verify_sparse(exported)
    mutate(criterion, lambda v: v.update(logprob_tolerance=0.001))
    mutate(exported / "rpc-007-error.json", lambda v: v.update(error="TimeoutError"))
    with pytest.raises(ValueError, match="checksum refusal"):
        summary.verify_sparse(exported)


def test_running_or_absent_scope_cannot_become_success(exported):
    mutate(exported / "scope.json", lambda v: v.update(status="running"))
    assert summary.verify_sparse(exported)["passed"] is False
    (exported / "scope.json").unlink()
    with pytest.raises(FileNotFoundError):
        summary.verify_sparse(exported)


def test_cli_writes_new_output_only_and_preserves_inputs(exported):
    before = {p.name: p.read_bytes() for p in exported.iterdir()}
    out = exported.parent / "offline-summary.json"
    command = [
        sys.executable,
        summary.__file__,
        "--root",
        str(exported),
        "--out",
        str(out),
    ]
    subprocess.run(command, check=True, capture_output=True)
    original_output = out.read_bytes()
    assert json.loads(original_output)["passed"] is True
    assert {p.name: p.read_bytes() for p in exported.iterdir()} == before
    second = subprocess.run(command, capture_output=True)
    assert second.returncode != 0
    assert out.read_bytes() == original_output


def test_cli_reports_incomplete_scope_with_nonzero_exit(exported):
    mutate(exported / "scope.json", lambda v: v.update(status="running"))
    out = exported.parent / "incomplete-summary.json"
    result = subprocess.run(
        [sys.executable, summary.__file__, "--root", str(exported), "--out", str(out)],
        capture_output=True,
    )
    assert result.returncode == 1
    assert json.loads(out.read_text())["passed"] is False


def test_unexpected_rpc_error_cannot_be_hidden_by_checksum_refusal(exported):
    write(exported / "rpc-008-error.json", {"error": "TimeoutError"})
    with pytest.raises(ValueError, match="checksum refusal"):
        summary.verify_sparse(exported)
