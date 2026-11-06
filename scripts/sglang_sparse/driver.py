"""Bounded offline native Engine proof; never allocates cloud resources."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import time
import uuid

MODEL = "deepseek-ai/DeepSeek-V4-Flash-0731"
REVISION = "7872f01b1d1fe23eabc4c98b48bffcef5a386062"
VERSION = "0.5.22.dev20261028+ge0df901d68"
ENGINE_KWARGS = {
    "trust_remote_code": True,
    "tp_size": 8,
    "pp_size": 1,
    "dp_size": 1,
    "dtype": "bfloat16",
    "attention_backend": "dsv4",
    "page_size": 256,
    "mem_fraction_static": 0.85,
    "swa_full_tokens_ratio": 0.15,
    "enforce_shared_experts_fusion": True,
    "kv_cache_dtype": "fp8_e4m3",
    "chunked_prefill_size": 2048,
    "context_length": 2048,
    "max_running_requests": 1,
    "disable_overlap_schedule": True,
    "disable_cuda_graph": True,
    "disable_radix_cache": True,
    "random_seed": 42,
    "log_level": "info",
}


def save(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False))


def preflight(overlay):
    import torch

    native = json.loads((overlay / "native-source-manifest.json").read_text())
    spec = importlib.util.find_spec("sglang")
    root = Path(spec.origin).parent
    profile = json.loads((overlay / "expected-profile.json").read_text())
    if profile["engine_kwargs"] != ENGINE_KWARGS or profile["distribution"] != VERSION:
        raise ValueError("Frozen startup contract differs from driver")
    if any(
        os.environ.get(key) != value for key, value in profile["environment"].items()
    ):
        raise ValueError("Frozen AMD startup environment differs")
    if any(key.startswith("SGLANG_SIMULATE_") for key in os.environ):
        raise ValueError("Simulation overrides forbidden")
    observed = {}
    for name, expected in native.items():
        path = root / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        observed[name] = actual
        if actual != expected:
            raise ValueError(f"Native source mismatch: {name}")
    for name, expected in json.loads(
        (overlay / "overlay-manifest.json").read_text()
    ).items():
        if hashlib.sha256((overlay / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Overlay source mismatch: {name}")
    if importlib.metadata.version("sglang") != VERSION or torch.version.hip is None:
        raise ValueError("Unsupported native SGLang/HIP runtime")
    if torch.cuda.device_count() != 8:
        raise ValueError("Expected eight MI355X devices")
    devices = []
    for rank in range(8):
        props = torch.cuda.get_device_properties(rank)
        if props.gcnArchName.split(":")[0] != "gfx950":
            raise ValueError("Expected gfx950")
        with torch.cuda.device(rank):
            x = torch.zeros(4, device=f"cuda:{rank}", dtype=torch.bfloat16)
            x.index_copy_(
                0,
                torch.tensor([1], device=x.device),
                torch.ones(1, device=x.device, dtype=x.dtype),
            )
            torch.cuda.synchronize(rank)
            if x.cpu().tolist() != [0, 1, 0, 0]:
                raise ValueError("Indexed kernel check failed")
            del x
            torch.cuda.empty_cache()
        devices.append(
            {
                "rank": rank,
                "architecture": props.gcnArchName,
                "total_memory": props.total_memory,
            }
        )
    return {
        "passed": True,
        "sources": observed,
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("sglang", "torch", "triton", "numpy", "xxhash", "zstandard")
        },
        "devices": devices,
        "hip": str(torch.version.hip),
    }


class Proof:
    def __init__(self, engine, out, deadline):
        self.engine, self.out, self.deadline = engine, out, deadline
        self.identities = None

    def rpc(self, operation, *, errors=False, **payload):
        if time.time() >= self.deadline - 60:
            raise TimeoutError("Insufficient work budget for next native operation")
        call = uuid.uuid4().hex
        started = time.monotonic()
        self.engine.collective_rpc(
            "ws_sparse_rpc",
            call_id=call,
            operation=operation,
            payload_json=json.dumps(payload, sort_keys=True),
        )
        records = []
        if len(list((self.out / "receipts").glob(f"{call}.*.json"))) != 8:
            raise ValueError("Expected exactly eight receipt files")
        for rank in range(8):
            record = json.loads(
                (self.out / "receipts" / f"{call}.{rank}.json").read_text()
            )
            if (
                any(
                    record[k] != v
                    for k, v in {
                        "schema_version": 1,
                        "call_id": call,
                        "method": operation,
                        "rank": rank,
                    }.items()
                )
                or type(record.get("ok")) is not bool
            ):
                raise ValueError("Receipt binding differs")
            records.append(record)
        save(
            self.out / f"call-{call}.json",
            {
                "operation": operation,
                "payload": payload,
                "elapsed_seconds": time.monotonic() - started,
                "records": records,
            },
        )
        if errors:
            return records
        if any(not record["ok"] for record in records):
            raise RuntimeError(f"Native rank failure: {records}")
        results = [record["result"] for record in records]
        if operation != "abort":
            if any(result.get("rank") != rank for rank, result in enumerate(results)):
                raise ValueError("Result rank differs from receipt rank")
            identities = {
                str(item["rank"]): item["identity_sha256"] for item in results
            }
            if (
                set(identities) != {str(rank) for rank in range(8)}
                or len(set(identities.values())) != 8
            ):
                raise ValueError("Incomplete or duplicate native identities")
            if self.identities is not None and identities != self.identities:
                raise ValueError("Native storage/process identities changed")
            self.identities = identities
        return results

    def generate(self, label):
        results = []
        for prompt in ("The capital of France is", "2 + 2 ="):
            if time.time() >= self.deadline - 120:
                raise TimeoutError("Insufficient generation budget")
            result = self.engine.generate(
                prompt,
                sampling_params={
                    "temperature": 0,
                    "max_new_tokens": 8,
                    "ignore_eos": True,
                },
                return_logprob=True,
                top_logprobs_num=5,
            )
            (self.out / f"generation-{label}-{len(results)}.raw.txt").write_text(
                repr(result)
            )
            validate_generation(result)
            results.append(result)
        save(self.out / f"generation-{label}.json", results)
        return results


def validate_generation(value):
    if type(value) is not dict or type(value.get("text")) is not str:
        raise ValueError("Malformed generation response")
    meta = value["meta_info"]
    selected, top = meta["output_token_logprobs"], meta["output_top_logprobs"]
    if len(selected) != 8 or len(top) != 8:
        raise ValueError("Incomplete generated logprobs")
    for chosen, candidates in zip(selected, top, strict=True):
        if (
            type(chosen[1]) is not int
            or not math.isfinite(chosen[0])
            or len(candidates) != 5
        ):
            raise ValueError("Invalid chosen token/logprobs")
        keys = [item[1] for item in candidates]
        if (
            len(set(keys)) != 5
            or any(type(key) is not int for key in keys)
            or any(not math.isfinite(item[0]) for item in candidates)
        ):
            raise ValueError("Invalid top logprobs")


def compare_generation(left, right):
    maximum, equal = 0.0, True
    for a, b in zip(left, right, strict=True):
        equal &= a["text"] == b["text"]
        for key in ("output_token_logprobs", "output_top_logprobs"):
            for x, y in zip(a["meta_info"][key], b["meta_info"][key], strict=True):
                x, y = ([x], [y]) if key == "output_token_logprobs" else (x, y)
                xd, yd = {v[1]: v[0] for v in x}, {v[1]: v[0] for v in y}
                equal &= set(xd) == set(yd)
                for token in set(xd) & set(yd):
                    maximum = max(maximum, abs(xd[token] - yd[token]))
    return {
        "passed": bool(equal and maximum <= 1e-3),
        "exact_text_tokens_topkeys": bool(equal),
        "maximum_logprob_error": maximum,
        "tolerance": 1e-3,
    }


def write_patch(root, baseline, version, parent, *, inverse=False):
    import torch
    from chalkcompute._weights_codec import TensorPatch, encode_patch, checksum
    from chalkcompute._weights_format import TensorEntry, WeightManifest
    from chalkcompute.weights.serving import sglang_resident_sparse as sparse

    root.mkdir()
    entries = []
    for name, shape in (
        (sparse.NORM, (sparse.HIDDEN,)),
        (sparse.HEAD, (sparse.VOCAB, sparse.HIDDEN)),
    ):
        raw, indices = bytearray(), []
        ranks = baseline[:1] if name == sparse.NORM else baseline
        if (
            name == sparse.NORM
            and len({rank["bytes"][name]["selected_hex"] for rank in baseline}) != 1
        ):
            raise ValueError("Replicated norm bytes differ")
        for rank in ranks:
            offset = (
                rank["rank"] * (sparse.VOCAB // 8) * sparse.HIDDEN
                if name == sparse.HEAD
                else 0
            )
            indices.extend(
                offset + index for index in rank["bytes"][name]["local_indices"]
            )
            data = bytearray.fromhex(rank["bytes"][name]["selected_hex"])
            if not inverse:
                for i in range(0, len(data), 2):
                    data[i] ^= 1
            raw.extend(data)
        values = torch.frombuffer(raw, dtype=torch.bfloat16).clone()
        if not torch.isfinite(values).all().item():
            raise ValueError("Fixture mutation nonfinite")
        data = encode_patch(
            TensorPatch(name, shape, "bfloat16", values, torch.tensor(indices))
        )
        filename = f"{len(entries)}.bin"
        (root / filename).write_bytes(data)
        entries.append(
            TensorEntry(
                name,
                "bfloat16",
                shape,
                "sparse",
                filename,
                0,
                len(data),
                len(indices),
                checksum(data),
            )
        )
    selected_hash = sparse._hash(
        {
            "profile": sparse.PROFILE,
            "tensors": [(entry.name, entry.shape, entry.dtype) for entry in entries],
        }
    )
    manifest = WeightManifest(
        version, parent, "patch", MODEL, selected_hash, tuple(entries)
    )
    (root / "manifest.json").write_bytes(manifest.to_json())
    return manifest


def check_bytes(current, baseline, *, changed, version=0):
    for actual, original in zip(current, baseline, strict=True):
        if actual["version"] != version:
            raise ValueError("Snapshot version differs")
        if actual["identity_sha256"] != original["identity_sha256"]:
            raise ValueError("Snapshot identity differs")
        for name, expected in original["bytes"].items():
            observed = actual["bytes"][name]
            raw = bytearray.fromhex(expected["selected_hex"])
            if changed:
                for i in range(0, len(raw), 2):
                    raw[i] ^= 1
            if (
                observed["selected_hex"] != raw.hex()
                or observed["untouched_sha256"] != expected["untouched_sha256"]
            ):
                raise ValueError("Selected or untouched bytes differ")
            if not changed and observed["sha256"] != expected["sha256"]:
                raise ValueError("Full inverse bytes differ")


def run(proof):
    proof.rpc("pause")
    proof.rpc("probe")
    baseline = proof.rpc("snapshot")
    proof.rpc("resume")
    a, repeat = proof.generate("A"), proof.generate("A-repeat")
    generation = {"A_repeat": compare_generation(a, repeat)}
    proof.rpc("pause")
    check_bytes(proof.rpc("snapshot"), baseline, changed=False)
    for version, inverse in ((1, False), (2, True)):
        patch = proof.out / f"patch-{version}"
        write_patch(patch, baseline, version, version - 1, inverse=inverse)
        transaction = uuid.uuid4().hex
        prepared = proof.rpc(
            "prepare",
            directory=str(patch),
            expected_version=version,
            transaction_id=transaction,
        )
        if any(
            item["version"] != version
            or item["parent_version"] != version - 1
            or item["local_nnz"] != {"model.norm.weight": 2, "lm_head.weight": 2}
            for item in prepared
        ):
            raise ValueError("Prepared version/count differs")
        digests = {item["manifest_sha256"] for item in prepared}
        expected = hashlib.sha256((patch / "manifest.json").read_bytes()).hexdigest()
        if digests != {expected}:
            raise ValueError("Prepared manifest hash differs")
        kwargs = dict(
            transaction_id=transaction,
            expected_version=version,
            rank_identities=proof.identities,
            manifest_sha256=expected,
        )
        committed = proof.rpc("commit", **kwargs)
        if any(
            item["version"] != version
            or len(item["writes"]) != 2
            or [write["nnz"] for write in item["writes"]] != [2, 2]
            or {write["data_ptr"] for write in item["writes"]}
            != {target["data_ptr"] for target in item["targets"].values()}
            for item in committed
        ):
            raise ValueError("Indexed writes/version differ")
        check_bytes(
            proof.rpc("snapshot"), baseline, changed=not inverse, version=version
        )
        proof.rpc("resume")
        observed = proof.generate(f"v{version}")
        proof.rpc("pause")
        check_bytes(
            proof.rpc("snapshot"), baseline, changed=not inverse, version=version
        )
        replay = proof.rpc("commit", **kwargs)
        if any(item["writes"] or item["version"] != version for item in replay):
            raise ValueError("Replay wrote resident bytes/version differs")
        check_bytes(
            proof.rpc("snapshot"), baseline, changed=not inverse, version=version
        )
        proof.rpc("resume")
        again = proof.generate(f"v{version}-replay")
        generation[f"v{version}_repeat"] = compare_generation(observed, again)
        if inverse:
            generation["inverse_A"] = compare_generation(observed, a)
        proof.rpc("pause")
        check_bytes(
            proof.rpc("snapshot"), baseline, changed=not inverse, version=version
        )
    corrupt = proof.out / "corrupt"
    write_patch(corrupt, baseline, 3, 2)
    data = bytearray((corrupt / "0.bin").read_bytes())
    data[-1] ^= 1
    (corrupt / "0.bin").write_bytes(data)
    transaction = uuid.uuid4().hex
    refused = proof.rpc(
        "prepare",
        errors=True,
        directory=str(corrupt),
        expected_version=3,
        transaction_id=transaction,
    )
    if any(
        item["ok"]
        or item.get("error_type") != "WeightStreamError"
        or item.get("error") != "Checksum mismatch for model.norm.weight"
        for item in refused
    ):
        raise ValueError("Corruption not explicitly refused by all original ranks")
    proof.rpc("abort", transaction_id=transaction)
    check_bytes(proof.rpc("snapshot"), baseline, changed=False, version=2)
    proof.rpc("resume")
    generation["corrupt_A"] = compare_generation(proof.generate("corrupt-refused"), a)
    proof.rpc("pause")
    check_bytes(proof.rpc("snapshot"), baseline, changed=False, version=2)
    return {
        "byte_control_passed": True,
        "generation": generation,
        "generation_passed": all(item["passed"] for item in generation.values()),
        "full_smoke_accepted": False,
        "scope": "offline native Engine TP8 BF16 norm/head only; no packed experts, HTTP drain, live publication or GPU-parent XOR claim",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--model-path", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--deadline-env", choices=["AMD_NATIVE_PHASE_DEADLINE"])
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    os.environ["AMD_EXPERIMENT_RUN_DIR"] = str(args.out)
    save(args.out / "preflight.json", preflight(args.overlay))
    if args.preflight_only:
        return
    deadline = float(os.environ[args.deadline_env]) if args.deadline_env else None
    if deadline is None or not math.isfinite(deadline) or time.time() >= deadline - 900:
        raise ValueError("Need >900s remaining native work budget")
    expected = (args.overlay / "expected-model-config.json").read_bytes()
    if (
        args.model_path is None
        or (args.model_path / "config.json").read_bytes() != expected
    ):
        raise ValueError("Exact prepared model config required")
    import sglang
    from scripts.sglang_sparse.native import run_scheduler_process

    engine = None

    def timeout(*unused):
        raise TimeoutError("Native phase absolute deadline reached")

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(min(600, int(deadline - time.time())))
    try:
        sglang.Engine.run_scheduler_process_func = staticmethod(run_scheduler_process)
        kwargs = {
            **ENGINE_KWARGS,
            "model_path": str(args.model_path),
            "revision": REVISION,
        }
        save(args.out / "engine-launch.json", kwargs)
        started = time.monotonic()
        engine = sglang.Engine(**kwargs)
        save(
            args.out / "startup.json",
            {
                "seconds": time.monotonic() - started,
                "effective_args": repr(engine.server_args),
            },
        )
        signal.alarm(max(1, int(deadline - time.time())))
        result = run(Proof(engine, args.out, deadline))
        save(args.out / "result.json", result)
        if not result["generation_passed"]:
            raise SystemExit(2)
    except BaseException as error:
        save(
            args.out / "failure.json",
            {"type": type(error).__name__, "error": str(error)},
        )
        raise
    finally:
        # Keep cleanup bounded even when the work deadline has just fired.
        signal.alarm(30)
        if engine is not None:
            engine.shutdown()
        signal.alarm(0)


if __name__ == "__main__":
    main()
