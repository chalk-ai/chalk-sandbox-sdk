"""Bounded read-only Kimi localization inside an independently owned native phase."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid

from scripts.vllm_sparse import proof
from scripts.vllm_sparse.pod_script import Experiment, write

MODE = "kimi_readonly_localization_v1"
PROMPTS = ([4, 5, 6], [6, 5, 4], [4], [5], [6], [4, 4, 4], [7, 8, 9], [127, 126, 125])
METHODS = {
    "ws_audit_runtime_info",
    "ws_audit_snapshot",
    "ws_localization_arm",
    "ws_localization_collect",
    "ws_localization_cancel",
}
BOUNDS = {
    "max_generation_responses": 58,
    "max_trace_requests": 8,
    "max_trace_bytes": 8 * 1024**2,
    "max_collective_trace_bytes": 65536,
    "max_selected_vocab_ids": 64,
    "max_activation_boundaries": 4,
    "max_trace_contexts": 2,
}


def validate_choice(response, prompt, count):
    if (
        type(response) is not dict
        or type(response.get("choices")) is not list
        or len(response["choices"]) != 1
    ):
        raise ValueError("Missing exactly one actual completion choice")
    choice = response["choices"][0]
    if choice.get("prompt_token_ids") != prompt or any(
        type(i) is not int for i in choice["prompt_token_ids"]
    ):
        raise ValueError("Actual returned prompt IDs differ from frozen context")
    ids = choice.get("token_ids")
    if (
        type(ids) is not list
        or not 1 <= len(ids) <= count
        or any(type(i) is not int or not 0 <= i < proof.VOCAB for i in ids)
    ):
        raise ValueError("Actual generated token IDs required")
    logprobs = choice.get("logprobs")
    if type(choice.get("text")) is not str or type(logprobs) is not dict:
        raise ValueError("Invalid completion text/logprob schema")
    if logprobs.get("tokens") != [f"token_id:{i}" for i in ids]:
        raise ValueError("Encoded token keys do not match actual generated IDs")
    scores, tops = logprobs.get("token_logprobs"), logprobs.get("top_logprobs")
    if (
        type(scores) is not list
        or type(tops) is not list
        or len(scores) != len(ids)
        or len(tops) != len(ids)
    ):
        raise ValueError("Invalid token/score array lengths")
    for token_id, score, top in zip(ids, scores, tops, strict=True):
        if type(top) is not dict or not top or len(top) > 6:
            raise ValueError("Invalid bounded top-logprob schema")
        for key, value in top.items():
            if (
                not key.startswith("token_id:")
                or not key[9:].isdigit()
                or not 0 <= int(key[9:]) < proof.VOCAB
            ):
                raise ValueError("Ambiguous decoded top-logprob key")
            if type(value) not in (float, int) or not math.isfinite(value):
                raise ValueError("Nonfinite top-logprob score")
        if (
            type(score) not in (float, int)
            or not math.isfinite(score)
            or top.get(f"token_id:{token_id}") != score
        ):
            raise ValueError("Chosen token/score identity differs within one response")
    return choice


def difference(before, after):
    left, right = before["logprobs"], after["logprobs"]
    ids_equal = before["token_ids"] == after["token_ids"]
    keys_equal = (
        all(
            a.keys() == b.keys()
            for a, b in zip(left["top_logprobs"], right["top_logprobs"], strict=True)
        )
        if len(left["top_logprobs"]) == len(right["top_logprobs"])
        else False
    )
    maximum = max(
        (abs(a - b) for a, b in zip(left["token_logprobs"], right["token_logprobs"])),
        default=0.0,
    )
    for a, b in zip(left["top_logprobs"], right["top_logprobs"]):
        maximum = max([maximum, *(abs(a[k] - b[k]) for k in a.keys() & b.keys())])
    return {
        "ids_equal": ids_equal,
        "top_id_sets_equal": keys_equal,
        "maximum_common_score_error": maximum,
        "different": not ids_equal or not keys_equal or maximum > 1e-3,
    }


def selected_ids(before, after):
    """Capture actual chosen/top IDs from the failing comparison, without decoding."""
    ids = set(before["token_ids"]) | set(after["token_ids"])
    for choice in (before, after):
        for top in choice["logprobs"]["top_logprobs"]:
            ids.update(int(key[9:]) for key in top)
    if len(ids) > BOUNDS["max_selected_vocab_ids"]:
        raise ValueError("Failing comparison exceeds selected vocabulary bound")
    return sorted(ids)


class ReadOnlyExperiment(Experiment):
    def __init__(self, directory, url):
        super().__init__(directory, url, diagnostic=True)
        self.generation_count = 0
        self.comparisons = []
        self.traces = []

    def request(self, endpoint, body):
        if endpoint == "/collective_rpc":
            if body.get("method") not in METHODS:
                raise ValueError(
                    "Mutation or unknown RPC forbidden in read-only localization"
                )
        elif endpoint == "/v1/completions":
            if self.generation_count >= BOUNDS["max_generation_responses"]:
                raise ValueError("Frozen generation response budget exhausted")
            self.generation_count += 1
        else:
            raise ValueError("Unknown endpoint forbidden in read-only localization")
        self.response_limit = (
            2 * 1024**2
            if endpoint == "/collective_rpc"
            and body.get("method") == "ws_audit_snapshot"
            else BOUNDS["max_collective_trace_bytes"]
        )
        if (
            sum(p.stat().st_size for p in self.directory.rglob("*") if p.is_file())
            > BOUNDS["max_trace_bytes"]
        ):
            raise ValueError("Read-only artifact budget exhausted before request")
        value = super().request(endpoint, body)
        if (
            sum(p.stat().st_size for p in self.directory.rglob("*") if p.is_file())
            > BOUNDS["max_trace_bytes"]
        ):
            raise ValueError(
                "Read-only artifact budget exceeded after bounded response"
            )
        return value

    def sample(self, label, prompt, count=1):
        request = {
            "model": os.environ["AMD_EXPERIMENT_MODEL"],
            "prompt": list(prompt),
            "max_tokens": count,
            "temperature": 0,
            "seed": 20261105,
            "logprobs": 5,
            "return_token_ids": True,
            "return_tokens_as_token_ids": True,
            "add_special_tokens": False,
        }
        response = self.request("/v1/completions", request)
        choice = validate_choice(response, list(prompt), count)
        write(
            self.directory / (label + ".json"),
            {
                "request": request,
                "response": response,
                "canonical_request_sha256": hashlib.sha256(
                    json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "raw_response_file": f"http-{self.sequence:03d}-raw.json",
                "raw_response_sha256": hashlib.sha256(
                    (self.directory / f"http-{self.sequence:03d}-raw.json").read_bytes()
                ).hexdigest(),
            },
        )
        return choice

    def compare(self, label, before, after, context):
        record = {
            "label": label,
            "context_token_ids": list(context),
            **difference(before, after),
        }
        self.comparisons.append(record)
        write(self.directory / "comparisons.json", self.comparisons)
        return record

    def trace_once(self, label, context, ids, layers, baseline):
        trace_id = uuid.uuid4().hex
        try:
            armed = self.rpc("ws_localization_arm", [trace_id, ids, layers])
            validate_trace_ranks(armed, baseline)
            if any(
                r.get("armed") is not True or r["trace_id"] != trace_id for r in armed
            ):
                raise ValueError("Not all ranks armed the exact read-only trace")
            choice = self.sample(label, context)
            captured = self.rpc("ws_localization_collect", [trace_id])
            validate_trace_ranks(captured, baseline)
            if (
                len(json.dumps(captured, allow_nan=False).encode())
                > BOUNDS["max_collective_trace_bytes"]
            ):
                raise ValueError("Aggregate trace exceeds64KiB collective budget")
            if any(r["trace_id"] != trace_id for r in captured):
                raise ValueError("Trace transaction identity differs")
            self.traces.append({"label": label, "ranks": captured})
            write(self.directory / "traces.json", self.traces)
            return choice, captured
        finally:
            cancelled = self.rpc("ws_localization_cancel", [])
            validate_trace_ranks(cancelled, baseline)
            if any(r.get("cancelled") is not True for r in cancelled):
                raise ValueError("Native trace wrapper restoration unacknowledged")


def validate_trace_ranks(records, baseline):
    old = {r["rank"]: r for r in baseline}
    if len(records) != 8 or {r["rank"] for r in records} != set(range(8)):
        raise ValueError("Missing eight distinct trace ranks")
    if any(
        type(r["rank"]) is not int or r["pid"] != old[r["rank"]]["pid"] for r in records
    ):
        raise ValueError("Native trace worker identity changed")


def first_layer_difference(left, right):
    differences = []
    for a, b in zip(
        sorted(left, key=lambda r: r["rank"]),
        sorted(right, key=lambda r: r["rank"]),
        strict=True,
    ):
        old = {
            r["layer"]: r["sha256"] for r in a["records"] if r["boundary"] == "layer"
        }
        new = {
            r["layer"]: r["sha256"] for r in b["records"] if r["boundary"] == "layer"
        }
        if old.keys() != new.keys():
            raise ValueError("Native traced layer sets differ")
        differences.extend(i for i in old if old[i] != new[i])
    return min(differences) if differences else None


def run(args, directory):
    if (
        hashlib.sha256(args.model_config.read_bytes()).hexdigest()
        != args.expected_config_sha256
    ):
        raise ValueError("Actual model config differs from frozen source contract")
    config = json.loads(args.model_config.read_bytes())["text_config"]
    layer_count = config["num_hidden_layers"]
    experiment = ReadOnlyExperiment(directory, os.environ["AMD_EXPERIMENT_SERVER_URL"])
    runtime = experiment.rpc("ws_audit_runtime_info", [])
    proof.validate_batch_records(runtime, proof.expected_runtime_profile())
    if any(r["version"] != 0 for r in runtime):
        raise ValueError("Read-only localization requires native sparse version0")
    write(directory / "effective-runtime.json", runtime)
    baseline = experiment.snapshot("A-before")
    failures = []
    for index, prompt in enumerate(PROMPTS):
        first = experiment.sample(f"immediate-{index}-a", prompt)
        second = experiment.sample(f"immediate-{index}-b", prompt)
        if experiment.compare(f"immediate-{index}", first, second, prompt)["different"]:
            failures.append((list(prompt), selected_ids(first, second)))
    for index, prompt in enumerate(PROMPTS):
        first = experiment.sample(f"intervening-{index}-a", prompt)
        experiment.sample(f"intervening-{index}-bait", PROMPTS[(index + 1) % 8])
        second = experiment.sample(f"intervening-{index}-b", prompt)
        if experiment.compare(f"intervening-{index}", first, second, prompt)[
            "different"
        ]:
            failures.append((list(prompt), selected_ids(first, second)))
    for index, prompt in enumerate(PROMPTS[:2]):
        anchor = experiment.sample(f"anchor-{index}", prompt, count=4)
        for position in range(2):
            context = list(prompt) + anchor["token_ids"][:position]
            first = experiment.sample(f"prefix-{index}-{position}-a", context)
            second = experiment.sample(f"prefix-{index}-{position}-b", context)
            if experiment.compare(f"prefix-{index}-{position}", first, second, context)[
                "different"
            ]:
                failures.append((context, selected_ids(first, second)))
    if failures:
        context, ids = failures[0]
        low, high = 0, layer_count - 1
        for step in range(4):
            layers = sorted(
                {low, low + (high - low) // 3, low + 2 * (high - low) // 3, high}
            )
            first, left = experiment.trace_once(
                f"trace-{step}-a", context, ids, layers, baseline
            )
            second, right = experiment.trace_once(
                f"trace-{step}-b", context, ids, layers, baseline
            )
            experiment.compare(f"trace-{step}", first, second, context)
            changed = first_layer_difference(left, right)
            if changed is None or low == high:
                break
            earlier = [i for i in layers if i < changed]
            low, high = (max(earlier) + 1 if earlier else low), changed
    final = experiment.snapshot("A-after")
    if final != baseline:
        raise ValueError(
            "Selected norm/head bytes or worker identities changed during read-only generation"
        )
    if (
        sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
        > BOUNDS["max_trace_bytes"]
    ):
        raise ValueError("Read-only diagnostic artifacts exceed8MiB budget")
    drift = any(r["different"] for r in experiment.comparisons)
    write(
        directory / "scope.json",
        {
            "status": "read_only_localization_complete",
            "mode": MODE,
            "full_smoke_accepted": False,
            "sparse_writes_requested": False,
            "selected_parameter_snapshots_exact": True,
            "whole_model_immutability_proven": False,
            "generation_responses": experiment.generation_count,
            "drift_observed": drift,
            "trace_pairs": len(experiment.traces) // 2,
            "bounds": BOUNDS,
            "limitations": [
                "Forced context HTTP requests exercise prefill, not incremental-decode equivalence",
                "Hashes locate byte divergence; they do not identify its cause",
                "Tracing changes timing; disappearance under trace is not a fix",
            ],
        },
    )
    return 2 if drift else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-reviewed", action="store_true")
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--expected-config-sha256", required=True)
    args = parser.parse_args()
    if not args.execute_reviewed:
        parser.error(
            "Requires separately frozen root-reviewed source/runtime/lifecycle contract"
        )
    directory = Path(os.environ["AMD_EXPERIMENT_RUN_DIR"]) / "readonly-localization"
    directory.mkdir()
    write(
        directory / "scope.json",
        {
            "status": "running",
            "mode": MODE,
            "full_smoke_accepted": False,
            "sparse_writes_requested": False,
        },
    )
    try:
        code = run(args, directory)
    except BaseException as error:
        write(
            directory / "failure.json",
            {
                "type": type(error).__name__,
                "error": str(error)[:2000],
                "at": time.time(),
            },
        )
        write(
            directory / "scope.json",
            {
                "status": "failed",
                "mode": MODE,
                "full_smoke_accepted": False,
                "sparse_writes_requested": False,
            },
        )
        raise
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
