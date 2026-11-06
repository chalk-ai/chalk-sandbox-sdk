"""One real sparse-patch experiment inside an already-owned pod; no lifecycle.

Root must stage the reviewed SDK + scripts package and provide loopback vLLM
with SparseAuditExtension. The amd-nc adapter owns locks/deadlines/evidence.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import struct
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

from scripts.vllm_sparse import proof


class NativeHTTPError(RuntimeError):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__(f"Native control HTTP{status}: {body}")


def write(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False))


class Experiment:
    def __init__(self, directory, url):
        self.directory, self.url = directory, url
        self.sequence = 0
        self.baseline = None

    def request(self, endpoint, body):
        self.sequence += 1
        request = urllib.request.Request(
            self.url + endpoint,
            json.dumps(body).encode(),
            {"Content-Type": "application/json"},
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read(2 * 1024**2 + 1)
            if len(raw) > 2 * 1024**2:
                raise ValueError("Control response exceeds2MiB")
            value = json.loads(raw)
            write(
                self.directory / f"rpc-{self.sequence:03d}.json",
                {
                    "endpoint": endpoint,
                    "request": body,
                    "response": value,
                    "started": started,
                    "finished": time.time(),
                },
            )
            return value
        except BaseException as error:
            error_body = None
            if isinstance(error, urllib.error.HTTPError):
                error_body = error.read(65537).decode(errors="replace")
            write(
                self.directory / f"rpc-{self.sequence:03d}-error.json",
                {
                    "endpoint": endpoint,
                    "request": body,
                    "error": type(error).__name__,
                    "message": str(error)[:1000],
                    "body": error_body,
                    "started": started,
                },
            )
            if error_body is not None:
                raise NativeHTTPError(error.code, error_body[:65536]) from error
            raise

    def rpc(self, method, args):
        result = self.request("/collective_rpc", {"method": method, "args": args})
        records = result.get("results") if isinstance(result, dict) else None
        if not isinstance(records, list) or len(records) != 8:
            raise ValueError("Missing eight native worker RPC results")
        return records

    def snapshot(self, label):
        records = self.rpc("ws_audit_snapshot", [proof.target_indices()])
        proof.validate_rank_records(records)
        if any(not r["torch_hip"] for r in records):
            raise ValueError("Actual ROCm workers required")
        write(self.directory / (label + ".json"), records)
        return records

    def generate(self, label):
        values = [
            self.request(
                "/v1/completions",
                {
                    "model": os.environ["AMD_EXPERIMENT_MODEL"],
                    "prompt": prompt,
                    "max_tokens": 8,
                    "temperature": 0,
                    "seed": 20261105,
                    "logprobs": 5,
                },
            )["choices"][0]
            for prompt in (
                [4, 5, 6],
                [6, 5, 4],
                [4],
                [5],
                [6],
                [4, 4, 4],
                [7, 8, 9],
                [127, 126, 125],
            )
        ]
        write(self.directory / (label + "-generation.json"), values)
        return values

    def apply(self, root, version, *, replay=None):
        transaction = replay or uuid.uuid4().hex
        prepared = self.rpc("ws_audit_prepare_dir", [str(root), version, transaction])
        ranks = {r["rank"]: r for r in prepared}
        if set(ranks) != set(range(8)) or any(
            r.get("prepared") is not True for r in ranks.values()
        ):
            raise ValueError("Not all eight ranks prepared; commit forbidden")
        manifest = json.loads((root / "manifest.json").read_bytes())
        baseline = proof.validate_rank_records(self.baseline)
        for rank, record in ranks.items():
            before = baseline[rank]
            if any(
                record.get(key) != value
                for key, value in {
                    "profile": "kimi-k3-bf16-tp8-v1",
                    "version": version,
                    "parent_version": manifest["parent_version"],
                    "tp_rank": rank,
                    "tp_world_size": 8,
                    "pid": before["pid"],
                    "pid_start_ticks": int(before["process_start_ticks"]),
                    "device": before["device"],
                    "model_id": before["model_id"],
                    "architecture_hash": manifest["architecture_hash"],
                }.items()
            ):
                raise ValueError(
                    "Prepared worker differs from pre-update runtime identity"
                )
            for name, target in record["targets"].items():
                for key in ("storage_ptr", "data_ptr", "shape"):
                    if target[key] != before["tensors"][name][key]:
                        raise ValueError(
                            "Prepared target storage differs before commit"
                        )
            if record["local_nnz"] != {proof.NORM: 4, proof.HEAD: 4}:
                raise ValueError(
                    "Every rank must prepare replicated norm and distinct TP shard coordinates"
                )
        hashes = {r["manifest_sha256"] for r in prepared}
        expected_hash = hashlib.sha256(
            (root / "manifest.json").read_bytes()
        ).hexdigest()
        if hashes != {expected_hash}:
            raise ValueError("Worker manifest digest conflict; commit forbidden")
        identities = {
            str(rank): record["identity_sha256"] for rank, record in ranks.items()
        }
        if len(set(identities.values())) != 8:
            raise ValueError("Worker identities are not distinct")
        committed = self.rpc(
            "ws_audit_commit", [transaction, version, identities, expected_hash]
        )
        if {r["rank"] for r in committed} != set(range(8)) or any(
            r["result"].get("committed") is not True for r in committed
        ):
            raise RuntimeError(
                "Uncertain commit: terminate all workers; no retry/fallback"
            )
        return transaction, identities, expected_hash


def samples(records):
    result = {proof.NORM: {}, proof.HEAD: {}}
    targets = proof.target_indices()
    for record in records:
        rank = record["rank"]
        for name, global_indices in targets.items():
            tensor = record["tensors"][name]
            local = proof.local_indices(name, global_indices, rank)
            selected = tensor.get("selected_base64")
            for global_index, local_index in zip(
                [
                    i
                    for i in global_indices
                    if name == proof.NORM
                    or i // ((proof.VOCAB // 8) * proof.WIDTH) == rank
                ],
                local,
                strict=True,
            ):
                if selected is None:
                    raw = base64.b64decode(tensor["base64"])
                    value = raw[local_index * 2 : local_index * 2 + 2]
                else:
                    value = base64.b64decode(selected[str(local_index)])
                if global_index in result[name] and result[name][global_index] != value:
                    raise ValueError("Replicated original parameter bytes conflict")
                result[name][global_index] = value
    return result


def build(directory, original, version, parent, xor, architecture_hash):
    import torch
    from chalkcompute._weights_codec import TensorPatch

    patches, desired = [], {}
    for name, indices in proof.target_indices().items():
        values = b"".join(
            struct.pack("<H", struct.unpack("<H", original[name][i])[0] ^ xor)
            for i in indices
        )
        bits = [
            struct.unpack("<H", values[i : i + 2])[0] for i in range(0, len(values), 2)
        ]
        if any(bit & 0x7F80 == 0x7F80 for bit in bits):
            raise ValueError("Nonfinite original or patch values unsupported")
        desired[name] = dict(
            zip(
                indices,
                [values[i : i + 2] for i in range(0, len(values), 2)],
                strict=True,
            )
        )
        shape = (proof.WIDTH,) if name == proof.NORM else (proof.VOCAB, proof.WIDTH)
        patches.append(
            TensorPatch(
                name,
                shape,
                "bfloat16",
                torch.frombuffer(bytearray(values), dtype=torch.bfloat16).clone(),
                torch.tensor(indices),
            )
        )
    proof.write_sparse_version(directory, version, parent, patches, architecture_hash)
    return desired


def check_bytes(baseline, observed, desired):
    proof.assert_stable_workers(baseline, observed)
    actual = samples(observed)
    if actual != desired:
        raise ValueError("Actual indexed readback differs from CPU sparse oracle")
    old = {r["rank"]: r for r in baseline}
    for record in observed:
        for name, tensor in record["tensors"].items():
            before = old[record["rank"]]["tensors"][name]
            if "masked_sha256" in tensor:
                if before["masked_sha256"] != tensor["masked_sha256"]:
                    raise ValueError("Unchanged TP-shard bytes changed")
            else:
                indices = proof.target_indices()[name]
                proof.assert_exact_sparse_bytes(
                    before["base64"],
                    tensor["base64"],
                    indices,
                    b"".join(desired[name][i] for i in indices),
                )


def compare_generation(reference, observed):
    maximum = 0.0
    if not reference or len(reference) != len(observed):
        raise ValueError("Generation sample count differs")
    for before, after in zip(reference, observed, strict=True):
        if (
            before["text"] != after["text"]
            or before["logprobs"]["tokens"] != after["logprobs"]["tokens"]
        ):
            raise ValueError("Unchanged/restored generation tokens differ")
        old, new = before["logprobs"], after["logprobs"]
        if not old["tokens"]:
            raise ValueError("Generation produced no tokens")
        for a, b in zip(old["token_logprobs"], new["token_logprobs"], strict=True):
            if not math.isfinite(a) or not math.isfinite(b):
                raise ValueError("Nonfinite generation logprob")
            maximum = max(maximum, abs(a - b))
        for a, b in zip(old["top_logprobs"], new["top_logprobs"], strict=True):
            if a.keys() != b.keys():
                raise ValueError("Unchanged/restored top logprob tokens differ")
            if not a or any(
                not math.isfinite(value) for value in [*a.values(), *b.values()]
            ):
                raise ValueError("Missing or nonfinite top logprobs")
            maximum = max(maximum, *(abs(a[key] - b[key]) for key in a))
    if maximum > 1e-3:
        raise ValueError(f"Fixed predeclared1e-3 criterion exceeded: {maximum}")
    return maximum


def corrupt_refusal(experiment, directory, transaction):
    try:
        experiment.rpc("ws_audit_prepare_dir", [str(directory), 4, transaction])
    except Exception as original:
        explicit = isinstance(original, NativeHTTPError) and (
            "Checksum mismatch for " + proof.NORM in original.body
        )
        try:
            experiment.rpc("ws_sparse_abort", [transaction])
        except Exception as abort_error:
            write(
                experiment.directory / "corruption-abort-error.json",
                {
                    "original_type": type(original).__name__,
                    "original_error": str(original),
                    "explicit_checksum_refusal": explicit,
                    "abort_error": str(abort_error),
                },
            )
            raise RuntimeError(
                "Abort failed after corrupt preparation; original refusal evidence retained"
            ) from original
        if not explicit:
            raise RuntimeError(
                "Corruption test failed for an unproven reason"
            ) from original
    else:
        raise ValueError("Corrupt payload was not rejected before commit")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-reviewed", action="store_true")
    parser.add_argument("--validate-runtime-only", action="store_true")
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--expected-config-sha256", required=True)
    args = parser.parse_args()
    if not args.execute_reviewed:
        parser.error(
            "Requires root-reviewed frozen source/runtime/manifest before execution"
        )
    directory = Path(os.environ["AMD_EXPERIMENT_RUN_DIR"]) / (
        "sparse-runtime-validation" if args.validate_runtime_only else "sparse-proof"
    )
    directory.mkdir()
    config_bytes = args.model_config.read_bytes()
    if hashlib.sha256(config_bytes).hexdigest() != args.expected_config_sha256:
        raise ValueError("Actual loaded model config differs from frozen config")
    architecture_hash = proof.selected_architecture_hash(config_bytes)
    write(
        directory / "selected-config-binding.json",
        {
            "config_sha256": args.expected_config_sha256,
            "selected_catalog_architecture_hash": architecture_hash,
            "scope": "actual config bytes plus two selected BF16 schemas, not full trainer architecture",
        },
    )
    experiment = Experiment(directory, os.environ["AMD_EXPERIMENT_SERVER_URL"])
    runtime = experiment.rpc("ws_audit_runtime_info", [])
    if len(runtime) != 8 or {record["rank"] for record in runtime} != set(range(8)):
        raise ValueError("Effective runtime config did not cover all eight workers")
    write(directory / "effective-runtime.json", runtime)
    if args.validate_runtime_only:
        return
    write(
        directory / "scope.json",
        {
            "status": "running",
            "sparse_tensor_readback_only": True,
            "generation_repeatability": "not_reached",
            "stream_drain": "not_proven",
            "fresh_B_native_oracle": "not_proven",
        },
    )
    baseline = experiment.snapshot("A")
    experiment.baseline = baseline
    by_rank = {record["rank"]: record for record in runtime}
    for record in baseline:
        earlier = by_rank[record["rank"]]
        if (
            earlier["version"] != 0
            or any(earlier[key] != record[key] for key in ("pid", "device", "model_id"))
            or earlier["pid_start_ticks"] != int(record["process_start_ticks"])
        ):
            raise ValueError(
                "Initial runtime config identity/version differs from baseline"
            )
    repeated = experiment.snapshot("A-unchanged-repeat")
    if baseline != repeated:
        raise ValueError("Unchanged tensor/worker snapshot was not exactly repeatable")
    baseline_generation = experiment.generate("A-cold")
    repeat_generation = experiment.generate("A-repeat")
    error = compare_generation(baseline_generation, repeat_generation)
    write(
        directory / "PREUPDATE-criterion.json",
        {
            "frozen_before_updates": True,
            "tensor_bytes": "exact",
            "generation_tokens": "exact",
            "logprob_tolerance": 1e-3,
            "observed_repeat_error": error,
            "prompts": 8,
            "tolerance_may_not_be_loosened": True,
        },
    )
    original = samples(baseline)
    last = baseline
    for label, version, parent, xor in [
        ("B", 1, 0, 1),
        ("C", 2, 1, 3),
        ("inverse_A", 3, 2, 0),
    ]:
        root = directory / label
        desired = build(root, original, version, parent, xor, architecture_hash)
        transaction, identities, digest = experiment.apply(root, version)
        observed = experiment.snapshot(label + "-after")
        check_bytes(baseline, observed, desired)
        generation = experiment.generate(label)
        if label == "inverse_A":
            compare_generation(baseline_generation, generation)
        replay = experiment.rpc(
            "ws_audit_commit", [transaction, version, identities, digest]
        )
        if any(
            r.get("idempotent_no_write_replay") is not True or r["sparse_writes"]
            for r in replay
        ):
            raise ValueError(
                "Exact commit replay was not a no-write idempotent acknowledgement"
            )
        replayed = experiment.snapshot(label + "-replay")
        check_bytes(baseline, replayed, desired)
        last = replayed
    corrupt = directory / "corrupt"
    build(corrupt, original, 4, 3, 1, architecture_hash)
    payload = corrupt / "patch-0.bin"
    data = bytearray(payload.read_bytes())
    data[-1] ^= 1
    payload.write_bytes(data)
    transaction = uuid.uuid4().hex
    corrupt_refusal(experiment, corrupt, transaction)
    final = experiment.snapshot("corruption-refusal")
    check_bytes(baseline, final, original)
    write(
        directory / "scope.json",
        {
            "status": "passed",
            "sparse_tensor_readback_only": True,
            "cases": [
                "A_B_C",
                "inverse",
                "idempotent_no_write_replay",
                "corrupt_preverify",
            ],
            "generation_repeatability": "passed_fixed_1e-3_before_updates_and_after_inverse",
            "stream_drain": "not_proven",
            "fresh_B_native_oracle": "not_proven",
            "last_snapshot_ranks": len(last),
        },
    )


if __name__ == "__main__":
    main()
