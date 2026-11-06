"""Check exported sparse byte/control receipts offline; no lifecycle attestation."""

import argparse
import base64
import hashlib
import json
import math
from pathlib import Path

NORM = "language_model.model.norm.weight"
HEAD = "language_model.lm_head.weight"
NORM_INDICES = [0, 1023, 3584, 7167]
WIDTH = 7168
HEAD_ROWS = 163840 // 8
HEAD_INDICES = [0, WIDTH - 1, (HEAD_ROWS - 1) * WIDTH, HEAD_ROWS * WIDTH - 1]


def read(path):
    return json.loads(path.read_text())


def ranks(values):
    if (
        len(values) != 8
        or any(type(r["rank"]) is not int or r["world_size"] != 8 for r in values)
        or {r["rank"] for r in values} != set(range(8))
    ):
        raise ValueError("Missing eight distinct ranks")
    if len({(r["pid"], r["process_start_ticks"]) for r in values}) != 8:
        raise ValueError("Missing eight distinct worker processes")
    if len({r["device"] for r in values}) != 8:
        raise ValueError("Missing eight distinct devices")
    return {r["rank"]: r for r in values}


def changed(raw, xor):
    if len(raw) != 2:
        raise ValueError("Expected one BF16 value")
    bits = int.from_bytes(raw, "little") ^ xor
    return bits.to_bytes(2, "little")


def norm_bytes(record):
    raw = base64.b64decode(record["base64"], validate=True)
    if len(raw) != WIDTH * 2 or hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise ValueError("Readback raw norm length/hash differs")
    return raw


def head_bytes(record):
    if record["omitted_indices"] != HEAD_INDICES or set(record["selected_base64"]) != {
        str(i) for i in HEAD_INDICES
    }:
        raise ValueError("Missing four exact local TP coordinates")
    result = {
        key: base64.b64decode(value, validate=True)
        for key, value in record["selected_base64"].items()
    }
    if any(len(value) != 2 for value in result.values()):
        raise ValueError("Head sample is not one BF16 value")
    digest = record["masked_sha256"]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Malformed unchanged-head digest")
    return result


def verify_prepare(root, rpc, baseline, version):
    """Bind every prepared rank to the exported manifest and original resident."""
    label = ("B", "C", "inverse_A")[version - 1]
    request = rpc["request"]["args"]
    if len(request) != 3 or request[1] != version or Path(request[0]).name != label:
        raise ValueError("Prepare order/version/directory differs")
    manifest_path = root / label / "manifest.json"
    manifest = read(manifest_path)
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if manifest["version"] != version or manifest["parent_version"] != version - 1:
        raise ValueError("Prepared manifest version differs")
    records = rpc["response"]["results"]
    if len(records) != 8 or {r["rank"] for r in records} != set(range(8)):
        raise ValueError("Missing eight prepared ranks")
    identities = {}
    for record in records:
        before = baseline[record["rank"]]
        expected = {
            "profile": "kimi-k3-bf16-tp8-v1",
            "tp_rank": record["rank"],
            "tp_world_size": 8,
            "pid": before["pid"],
            "pid_start_ticks": int(before["process_start_ticks"]),
            "device": before["device"],
            "model_id": before["model_id"],
            "transaction_id": request[2],
            "version": version,
            "parent_version": version - 1,
            "manifest_sha256": digest,
            "architecture_hash": manifest["architecture_hash"],
            "local_nnz": {NORM: 4, HEAD: 4},
            "compressed_payload_bytes": sum(t["length"] for t in manifest["tensors"]),
            "global_sparse_values_bytes": 72,
            "global_logical_int64_index_bytes": 288,
        }
        if (
            type(record["rank"]) is not int
            or record.get("prepared") is not True
            or record.get("ok") is not True
            or any(record.get(key) != value for key, value in expected.items())
        ):
            raise ValueError(
                "Prepared worker/manifest identity or sparse counts differ"
            )
        if set(record["targets"]) != {NORM, HEAD}:
            raise ValueError("Prepared target set differs")
        for name, target in record["targets"].items():
            if any(
                target[key] != before["tensors"][name][key]
                for key in ("storage_ptr", "data_ptr", "shape")
            ):
                raise ValueError("Prepared target storage differs")
        elapsed = record["prepare_verify_and_stage_seconds"]
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("Invalid prepare timing")
        identities[str(record["rank"])] = record["identity_sha256"]
    if len(set(identities.values())) != 8:
        raise ValueError("Prepared rank identities are not distinct")
    return [request[2], version, identities, digest]


def verify_sparse(root):
    scope = read(root / "scope.json")
    if scope.get("status") != "passed":
        return {
            "passed": False,
            "scope_record": scope,
            "reason": "Native harness did not report passed",
        }
    baseline = ranks(read(root / "A.json"))
    if read(root / "A.json") != read(root / "A-unchanged-repeat.json"):
        raise ValueError("Baseline was not exactly repeatable")
    for record in baseline.values():
        pointers = [record["tensors"][name]["storage_ptr"] for name in (NORM, HEAD)]
        if len(set(pointers)) != 2 or any(
            type(p) is not int or p <= 0 for p in pointers
        ):
            raise ValueError(
                "Inspected resident storage pointers must be distinct and positive"
            )
        for name, shape in ((NORM, [WIDTH]), (HEAD, [HEAD_ROWS, WIDTH])):
            tensor = record["tensors"][name]
            if tensor["shape"] != shape or tensor["dtype"] != "bfloat16":
                raise ValueError("Inspected tensor schema differs")
        norm_bytes(record["tensors"][NORM])
        head_bytes(record["tensors"][HEAD])
    if len({r["tensors"][NORM]["base64"] for r in baseline.values()}) != 1:
        raise ValueError("Replicated baseline norm differs")
    checks = []
    for label, xor in [
        ("B-after", 1),
        ("B-replay", 1),
        ("C-after", 3),
        ("C-replay", 3),
        ("inverse_A-after", 0),
        ("inverse_A-replay", 0),
        ("corruption-refusal", 0),
    ]:
        observed = ranks(read(root / (label + ".json")))
        for rank, before in baseline.items():
            after = observed[rank]
            for key in (
                "pid",
                "process_start_ticks",
                "device",
                "device_uuid",
                "model_id",
            ):
                if before[key] != after[key]:
                    raise ValueError("Worker identity changed: " + key)
            for name in (NORM, HEAD):
                old, new = before["tensors"][name], after["tensors"][name]
                for key in ("shape", "dtype", "storage_ptr", "data_ptr"):
                    if old[key] != new[key]:
                        raise ValueError("Resident parameter identity changed")
                if name == NORM:
                    original = norm_bytes(old)
                    actual = norm_bytes(new)
                    expected = bytearray(original)
                    for index in NORM_INDICES:
                        expected[index * 2 : index * 2 + 2] = changed(
                            original[index * 2 : index * 2 + 2], xor
                        )
                    if bytes(expected) != actual:
                        raise ValueError(
                            "Norm changed/unchanged bytes differ from independent XOR oracle"
                        )
                else:
                    if (
                        old["masked_sha256"] != new["masked_sha256"]
                        or old["omitted_indices"] != new["omitted_indices"]
                    ):
                        raise ValueError("Unchanged head-shard digest differs")
                    original, actual = head_bytes(old), head_bytes(new)
                    for index, value in original.items():
                        if actual[index] != changed(value, xor):
                            raise ValueError(
                                "Head selected value differs from independent XOR oracle"
                            )
        checks.append(label)
    criterion = read(root / "PREUPDATE-criterion.json")
    if (
        criterion.get("logprob_tolerance") != 0.001
        or criterion.get("frozen_before_updates") is not True
    ):
        raise ValueError("Fixed pre-update criterion missing")
    commit_calls = []
    errors = []
    previous_request = None
    prepares = []
    for path in sorted(root.glob("rpc-*.json")):
        value = read(path)
        if path.name.endswith("-error.json"):
            errors.append(value)
            continue
        method = value["request"].get("method")
        if method == "ws_audit_prepare_dir":
            version = len(prepares) + 1
            if version > 3 or len(commit_calls) != (version - 1) * 2:
                raise ValueError("Prepare barrier order differs")
            prepares.append(verify_prepare(root, value, baseline, version))
            continue
        if method != "ws_audit_commit":
            continue
        workers = value["response"]["results"]
        if (
            {r["rank"] for r in workers} != set(range(8))
            or len(workers) != 8
            or any(type(r["rank"]) is not int for r in workers)
        ):
            raise ValueError("Incomplete commit rank coverage")
        call_index = len(commit_calls)
        expected_version, expected_replay = call_index // 2 + 1, bool(call_index % 2)
        request = value["request"]["args"]
        if len(request) != 4 or request[1] != expected_version:
            raise ValueError("Commit order/version differs")
        if len(prepares) != expected_version or request != prepares[-1]:
            raise ValueError("Commit lacks matching all-rank prepare barrier")
        if (
            set(request[2]) != {str(rank) for rank in range(8)}
            or len(set(request[2].values())) != 8
        ):
            raise ValueError("Commit is not bound to eight distinct rank identities")
        if expected_replay and request != previous_request:
            raise ValueError("Replay transaction identity differs")
        previous_request = request
        rpc_seconds = value["finished"] - value["started"]
        if not math.isfinite(rpc_seconds) or rpc_seconds < 0:
            raise ValueError("Invalid controller RPC timing")
        for worker in workers:
            ack = worker["result"]
            if (
                worker["pid"] != baseline[worker["rank"]]["pid"]
                or ack.get("committed") is not True
                or ack.get("ok") is not True
                or ack.get("prepared") is not False
                or ack.get("transaction_id") != request[0]
                or ack.get("version") != expected_version
                or ack.get("parent_version") != expected_version - 1
                or ack.get("manifest_sha256") != request[3]
                or ack.get("identity_sha256") != request[2][str(worker["rank"])]
                or worker["full_loaders_forbidden"] is not True
                or worker["idempotent_no_write_replay"] is not expected_replay
            ):
                raise ValueError("Commit acknowledgement/worker identity differs")
            elapsed = worker["commit_and_sync_seconds"]
            if (
                not math.isfinite(elapsed)
                or elapsed < 0
                or not math.isfinite(worker["maximum_new_storage_bytes"])
                or worker["maximum_new_storage_bytes"] < 0
                or worker["maximum_new_storage_bytes"] > 4096
            ):
                raise ValueError("Commit timing/allocation instrumentation failed")
            writes = worker["sparse_writes"]
            if worker["idempotent_no_write_replay"]:
                if writes:
                    raise ValueError("Replay wrote native model storage")
            else:
                if len(writes) != 2:
                    raise ValueError("Missing norm and head resident indexed writes")
                pointers = {
                    baseline[worker["rank"]]["tensors"][name]["storage_ptr"]
                    for name in (NORM, HEAD)
                }
                if {w["storage_ptr"] for w in writes} != pointers or any(
                    w["op"] != "aten.index_copy_.default" for w in writes
                ):
                    raise ValueError(
                        "Native sparse operation did not target expected resident storage"
                    )
        commit_calls.append(
            {
                "version": value["request"]["args"][1],
                "replay": all(w["idempotent_no_write_replay"] for w in workers),
                "maximum_worker_commit_and_sync_seconds": max(
                    w["commit_and_sync_seconds"] for w in workers
                ),
                "controller_rpc_seconds": rpc_seconds,
            }
        )
    if len(commit_calls) != 6 or len(prepares) != 3:
        raise ValueError("Missing six commit/replay control calls")
    if len(errors) != 1 or not all(
        v.get("error") == "HTTPError"
        and v.get("request", {}).get("method") == "ws_audit_prepare_dir"
        and v["request"]["args"][1] == 4
        and "Checksum mismatch for " + NORM in (v.get("body") or "")
        for v in errors
    ):
        raise ValueError("Explicit native checksum refusal body missing")
    return {
        "passed": True,
        "verification_scope": "exported sparse byte/control receipts only",
        "rank_count": 8,
        "snapshot_checks": checks,
        "commit_calls": commit_calls,
        "all_rank_prepare_barriers": len(prepares),
        "scope_record": scope,
        "limits": [
            "Trusts exported worker instrumentation; does not authenticate image/source or allocation",
            "Checks only inspected norm/head tensors; unchanged head bytes retained as native streamed hashes",
            "Does not independently verify cleanup, stream drain or fresh-B oracle",
            "Generation samples retained separately; this summarizer checks byte/control evidence only",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, required=True, help="Exported sparse-proof directory"
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = verify_sparse(args.root)
    with args.out.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result, indent=2))
    if result["passed"] is not True:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
