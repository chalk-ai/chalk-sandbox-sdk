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


def commit_path(root, call):
    return root / f"rpc-{call + call // 2 + 1:03d}.json"


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
        label = ("B", "C", "inverse_A")[version - 1]
        manifest_path = tmp_path / label / "manifest.json"
        if not replay:
            manifest_path.parent.mkdir()
            write(
                manifest_path,
                {
                    "version": version,
                    "parent_version": version - 1,
                    "architecture_hash": "synthetic-architecture",
                    "tensors": [{"length": 101}, {"length": 105}],
                },
            )
        digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if not replay:
            prepared = []
            for rank, before in enumerate(baseline):
                prepared.append(
                    {
                        "rank": rank,
                        "profile": "kimi-k3-bf16-tp8-v1",
                        "tp_rank": rank,
                        "tp_world_size": 8,
                        "pid": before["pid"],
                        "pid_start_ticks": int(before["process_start_ticks"]),
                        "device": before["device"],
                        "model_id": before["model_id"],
                        "targets": copy.deepcopy(before["tensors"]),
                        "ok": True,
                        "prepared": True,
                        "transaction_id": f"transaction-{version}",
                        "version": version,
                        "parent_version": version - 1,
                        "manifest_sha256": digest,
                        "architecture_hash": "synthetic-architecture",
                        "identity_sha256": identities[str(rank)],
                        "local_nnz": {summary.NORM: 4, summary.HEAD: 4},
                        "compressed_payload_bytes": 206,
                        "global_sparse_values_bytes": 72,
                        "global_logical_int64_index_bytes": 288,
                        "prepare_verify_and_stage_seconds": 0.02,
                    }
                )
            write(
                tmp_path / f"rpc-{call + call // 2:03d}.json",
                {
                    "request": {
                        "method": "ws_audit_prepare_dir",
                        "args": [
                            f"/remote/sparse-proof/{label}",
                            version,
                            f"transaction-{version}",
                        ],
                    },
                    "response": {"results": prepared},
                },
            )
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
                        "manifest_sha256": digest,
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
            commit_path(tmp_path, call),
            {
                "request": {
                    "method": "ws_audit_commit",
                    "args": [
                        f"transaction-{version}",
                        version,
                        identities,
                        digest,
                    ],
                },
                "response": {"results": workers},
                "started": 100.0 + call,
                "finished": 100.2 + call,
            },
        )
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    payload = b"synthetic corrupted norm stream"
    (corrupt / "patch-0.bin").write_bytes(payload)
    write(
        corrupt / "manifest.json",
        {
            "version": 4,
            "parent_version": 3,
            "tensors": [
                {
                    "name": summary.NORM,
                    "file": "patch-0.bin",
                    "offset": 0,
                    "length": len(payload),
                    "checksum": "0" * 32,
                }
            ],
        },
    )
    refusals = copy.deepcopy(prepared)
    for record in refusals:
        record.update(
            version=3,
            version_after=3,
            requested_version=4,
            transaction_id="refusal-transaction",
            refused=True,
            error_type="WeightStreamError",
            error="Checksum mismatch for " + summary.NORM,
            corrupt_tensor=summary.NORM,
            manifest_sha256=hashlib.sha256(
                (corrupt / "manifest.json").read_bytes()
            ).hexdigest(),
            expected_checksum="0" * 32,
            actual_checksum="1" * 32,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            pending_before=False,
            pending_after=False,
            failed_before=False,
            failed_after=False,
            sparse_writes=[],
        )
    write(
        tmp_path / "rpc-009.json",
        {
            "request": {
                "method": "ws_audit_corrupt_refusal",
                "args": ["/remote/corrupt", 4, "refusal-transaction"],
            },
            "response": {"results": refusals},
        },
    )
    write(
        tmp_path / "rpc-010.json",
        {
            "request": {"method": "ws_sparse_abort", "args": ["refusal-transaction"]},
            "response": {
                "results": [
                    {
                        "ok": True,
                        "aborted": True,
                        "transaction_id": "refusal-transaction",
                    }
                ]
                * 8
            },
        },
    )
    return tmp_path


def test_valid_export_checks_bytes_controls_and_retains_scope_limits(exported):
    result = summary.verify_sparse(exported)
    assert result["passed"] is True
    assert result["rank_count"] == 8
    assert result["all_rank_prepare_barriers"] == 3
    assert result["checksum_refusal_ranks"] == 8
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
        commit_path(exported, call),
        lambda rpc: rpc["response"]["results"][0].update({key: value}),
    )
    with pytest.raises(ValueError):
        summary.verify_sparse(exported)


def test_reject_replay_transaction_change(exported):
    mutate(
        commit_path(exported, 1),
        lambda rpc: rpc["request"]["args"].__setitem__(0, "other"),
    )
    with pytest.raises(ValueError, match="prepare barrier"):
        summary.verify_sparse(exported)


@pytest.mark.parametrize(
    "key", ["version", "transaction_id", "manifest_sha256", "identity_sha256"]
)
def test_commit_ack_must_match_requested_version_and_identities(exported, key):
    mutate(
        commit_path(exported, 0),
        lambda rpc: rpc["response"]["results"][0]["result"].update({key: "conflict"}),
    )
    with pytest.raises(ValueError, match="acknowledgement"):
        summary.verify_sparse(exported)


def test_commit_requires_complete_distinct_rank_identity_map(exported):
    mutate(
        commit_path(exported, 0), lambda rpc: rpc["request"]["args"].__setitem__(2, {})
    )
    with pytest.raises(ValueError, match="prepare barrier"):
        summary.verify_sparse(exported)


def test_reject_missing_rank_and_missing_rpc(exported):
    mutate(commit_path(exported, 0), lambda rpc: rpc["response"]["results"].pop())
    with pytest.raises(ValueError, match="rank coverage"):
        summary.verify_sparse(exported)
    commit_path(exported, 0).unlink()
    with pytest.raises(ValueError):
        summary.verify_sparse(exported)


def test_reject_loosened_criterion_and_generic_corruption_error(exported):
    criterion = exported / "PREUPDATE-criterion.json"
    mutate(criterion, lambda v: v.update(logprob_tolerance=0.01))
    with pytest.raises(ValueError, match="criterion"):
        summary.verify_sparse(exported)
    mutate(criterion, lambda v: v.update(logprob_tolerance=0.001))
    write(exported / "rpc-011-error.json", {"error": "TimeoutError"})
    with pytest.raises(ValueError, match="Unexpected RPC error"):
        summary.verify_sparse(exported)


def test_running_or_absent_scope_cannot_become_success(exported):
    mutate(exported / "scope.json", lambda v: v.update(status="running"))
    assert summary.verify_sparse(exported)["passed"] is False
    (exported / "scope.json").unlink()
    with pytest.raises(FileNotFoundError):
        summary.verify_sparse(exported)


def test_cli_writes_new_output_only_and_preserves_inputs(exported):
    before = {
        str(p.relative_to(exported)): p.read_bytes()
        for p in exported.rglob("*")
        if p.is_file()
    }
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
    assert {
        str(p.relative_to(exported)): p.read_bytes()
        for p in exported.rglob("*")
        if p.is_file()
    } == before
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
    write(
        exported / "rpc-011-error.json",
        {"error": "HTTPError", "body": "Checksum mismatch for " + summary.NORM},
    )
    with pytest.raises(ValueError, match="Unexpected RPC error"):
        summary.verify_sparse(exported)


def test_missing_prepare_barriers_cannot_pass_with_complete_commits(exported):
    for sequence in (0, 3, 6):
        (exported / f"rpc-{sequence:03d}.json").unlink()
    with pytest.raises(ValueError, match="prepare barrier"):
        summary.verify_sparse(exported)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("prepared", False),
        ("pid", 999),
        ("pid_start_ticks", 999),
        ("device", "cuda:7"),
        ("model_id", 999),
        ("manifest_sha256", "conflict"),
        ("identity_sha256", "other-identity"),
        ("local_nnz", {summary.NORM: 4, summary.HEAD: 0}),
        ("prepare_verify_and_stage_seconds", float("nan")),
    ],
)
def test_prepare_must_bind_all_rank_resident_identity_and_sparse_counts(
    exported, key, value
):
    mutate(
        exported / "rpc-000.json",
        lambda rpc: rpc["response"]["results"][0].update({key: value}),
    )
    with pytest.raises(ValueError):
        summary.verify_sparse(exported)


def test_prepare_requires_every_rank_and_exact_target_storage(exported):
    prepare = exported / "rpc-000.json"
    original = prepare.read_bytes()
    mutate(prepare, lambda rpc: rpc["response"]["results"].pop())
    with pytest.raises(ValueError, match="prepared ranks"):
        summary.verify_sparse(exported)
    prepare.write_bytes(original)
    mutate(
        prepare,
        lambda rpc: rpc["response"]["results"][0]["targets"][summary.HEAD].update(
            storage_ptr=999
        ),
    )
    with pytest.raises(ValueError, match="target storage"):
        summary.verify_sparse(exported)


def test_prepare_manifest_digest_must_match_exported_manifest(exported):
    mutate(
        exported / "B/manifest.json",
        lambda manifest: manifest.update(architecture_hash="other"),
    )
    with pytest.raises(ValueError, match="manifest identity"):
        summary.verify_sparse(exported)


@pytest.mark.parametrize("pointer", [0, -1, 1000])
def test_baseline_requires_positive_distinct_norm_and_head_storage(exported, pointer):
    for name in ("A", "A-unchanged-repeat"):
        mutate(
            exported / f"{name}.json",
            lambda rows: rows[0]["tensors"][summary.HEAD].update(
                storage_ptr=pointer, data_ptr=pointer
            ),
        )
    with pytest.raises(ValueError, match="distinct and positive"):
        summary.verify_sparse(exported)


def test_substring_index_copy_name_is_not_native_indexed_write_evidence(exported):
    mutate(
        commit_path(exported, 0),
        lambda rpc: rpc["response"]["results"][0]["sparse_writes"][0].update(
            op="fake.index_copy_.default"
        ),
    )
    with pytest.raises(ValueError, match="Native sparse operation"):
        summary.verify_sparse(exported)


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "missing_rank",
        "version",
        "tx",
        "identity",
        "full_loader",
        "allocation",
        "timing",
        "operation",
        "storage",
        "replay_write",
    ],
)
def test_diagnostic_online_commit_gate_reuses_real_receipt_contract(exported, damage):
    from scripts.vllm_sparse import pod_script as pod

    replay = damage == "replay_write"
    rpc = json.loads(commit_path(exported, 1 if replay else 0).read_text())
    records = rpc["response"]["results"]
    transaction, version, identities, digest = rpc["request"]["args"]
    if damage == "missing_rank":
        records.pop()
    elif damage == "version":
        records[-1]["result"]["version"] = True
    elif damage == "tx":
        records[-1]["result"]["transaction_id"] = "wrong"
    elif damage == "identity":
        records[-1]["result"]["identity_sha256"] = "wrong"
    elif damage == "full_loader":
        records[-1]["full_loaders_forbidden"] = False
    elif damage == "allocation":
        records[-1]["maximum_new_storage_bytes"] = 4097
    elif damage == "timing":
        records[-1]["commit_and_sync_seconds"] = float("nan")
    elif damage == "operation":
        records[-1]["sparse_writes"][0]["op"] = "wrong"
    elif damage == "storage":
        records[-1]["sparse_writes"][0]["storage_ptr"] = 0
    elif damage == "replay_write":
        records[-1]["sparse_writes"] = [
            {"op": "aten.index_copy_.default", "storage_ptr": 123}
        ]
    baseline = json.loads((exported / "A.json").read_text())
    if damage is None:
        pod.validate_diagnostic_commit(
            records, baseline, version, transaction, identities, digest, replay=replay
        )
    else:
        with pytest.raises(ValueError):
            pod.validate_diagnostic_commit(
                records,
                baseline,
                version,
                transaction,
                identities,
                digest,
                replay=replay,
            )


def test_diagnostic_byte_checker_does_not_make_diagnostic_a_strict_pass(exported):
    scope = {"status": "diagnostic_complete", "full_smoke_accepted": False}
    write(exported / "scope.json", scope)
    assert summary.verify_byte_control_receipts(exported, scope)["passed"] is True
    assert summary.verify_sparse(exported)["passed"] is False
    mutate(
        commit_path(exported, 0),
        lambda rpc: rpc["response"]["results"][-1].update(full_loaders_forbidden=False),
    )
    with pytest.raises(ValueError):
        summary.verify_byte_control_receipts(exported, scope)
