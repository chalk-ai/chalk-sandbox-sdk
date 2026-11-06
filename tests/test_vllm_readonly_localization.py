"""CPU guards and real Torch observer tests; no native compatibility claim."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
import torch

from scripts.vllm_drift import probe, trace


def response(prompt=(4, 5, 6), ids=(7,), score=-1.0):
    return {
        "choices": [
            {
                "text": "a",
                "prompt_token_ids": list(prompt),
                "token_ids": list(ids),
                "logprobs": {
                    "tokens": [f"token_id:{i}" for i in ids],
                    "token_logprobs": [score] * len(ids),
                    "top_logprobs": [{f"token_id:{i}": score} for i in ids],
                },
            }
        ]
    }


@pytest.mark.parametrize(
    "damage",
    [
        "null_ids",
        "decoded",
        "prompt",
        "bool",
        "nonfinite",
        "collision",
        "keys",
        "two_choices",
    ],
)
def test_token_identity_and_schema_fail_closed(damage):
    value = response()
    choice = value["choices"][0]
    if damage == "null_ids":
        choice["token_ids"] = None
    elif damage == "decoded":
        choice["logprobs"]["tokens"] = [""]
    elif damage == "prompt":
        choice["prompt_token_ids"] = [6, 5, 4]
    elif damage == "bool":
        choice["token_ids"] = [True]
    elif damage == "nonfinite":
        choice["logprobs"]["token_logprobs"] = [float("nan")]
    elif damage == "collision":
        choice["logprobs"]["top_logprobs"][0]["token_id:7"] = -2.0
    elif damage == "keys":
        choice["logprobs"]["top_logprobs"] = [{"": -1.0}]
    else:
        value["choices"].append(copy.deepcopy(choice))
    with pytest.raises(ValueError):
        probe.validate_choice(value, [4, 5, 6], 1)


def test_actual_encoded_ids_and_disjoint_top_sets_compare_without_decode():
    left = probe.validate_choice(response(), [4, 5, 6], 1)
    right = probe.validate_choice(response(ids=(8,)), [4, 5, 6], 1)
    result = probe.difference(left, right)
    assert (
        result["different"]
        and not result["ids_equal"]
        and not result["top_id_sets_equal"]
    )
    assert probe.difference(left, left)["different"] is False


@pytest.mark.parametrize(
    "method",
    [
        "ws_sparse_prepare_dir",
        "ws_audit_prepare_dir",
        "ws_sparse_commit",
        "ws_audit_commit",
        "ws_apply_dir",
        "unknown",
    ],
)
def test_mutation_rpcs_rejected_before_transport(tmp_path, monkeypatch, method):
    monkeypatch.setattr(
        probe.Experiment,
        "request",
        lambda *args: pytest.fail("Transport must not be entered"),
    )
    experiment = probe.ReadOnlyExperiment(tmp_path, "http://unused.invalid")
    with pytest.raises(ValueError, match="forbidden"):
        experiment.rpc(method, [])


def test_generation_budget_precedes_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(
        probe.Experiment,
        "request",
        lambda *args: pytest.fail("Transport must not be entered"),
    )
    experiment = probe.ReadOnlyExperiment(tmp_path, "http://unused.invalid")
    experiment.generation_count = 58
    with pytest.raises(ValueError, match="budget"):
        experiment.request("/v1/completions", {})


class Sampler(torch.nn.Module):
    logprobs_mode = "raw_logprobs"

    def forward(self, logits):
        ids = logits.argmax(-1, keepdim=True)
        values = logits.log_softmax(-1).gather(-1, ids)
        return SimpleNamespace(
            sampled_token_ids=ids,
            logprobs_tensors=SimpleNamespace(logprob_token_ids=ids, logprobs=values),
        )


class Model(torch.nn.Module):
    def __init__(self, *, fail=False):
        super().__init__()
        self.layers = torch.nn.ModuleList([torch.nn.Identity() for _ in range(4)])
        self.language_model = SimpleNamespace(model=SimpleNamespace(layers=self.layers))
        self.fail = fail

    def compute_logits(self, hidden_states):
        if self.fail:
            raise RuntimeError("actual compute failure")
        return hidden_states

    def forward(self, hidden):
        for layer in self.layers:
            hidden = layer(hidden)
        return self.compute_logits(hidden)


def worker(model):
    return SimpleNamespace(
        model_runner=SimpleNamespace(get_model=lambda: model, sampler=Sampler())
    )


def test_real_torch_hooks_preserve_outputs_inputs_and_restore_exact_method():
    model = Model()
    target = worker(model)
    original = model.compute_logits.__func__
    inputs = torch.arange(16, dtype=torch.float32).reshape(1, 16)
    saved = inputs.clone()
    trace_id = "a" * 32
    assert trace.arm(target, trace_id, [1, 7], [0, 3])["armed"]
    result = model(inputs)
    target.model_runner.sampler(result)
    captured = trace.collect(target, trace_id)
    assert result is inputs
    assert torch.equal(inputs, saved)
    assert model.compute_logits.__func__ is original
    assert "compute_logits" not in model.__dict__
    assert not hasattr(target, "_ws_readonly_trace")
    assert {r["boundary"] for r in captured["records"]} == {
        "layer",
        "pre_final_norm",
        "raw_logits",
        "sampler",
    }
    assert [r["layer"] for r in captured["records"] if r["boundary"] == "layer"] == [
        0,
        3,
    ]
    assert all(len(layer._forward_hooks) == 0 for layer in model.layers)


@pytest.mark.parametrize("failure", ["compute", "nonfinite", "second_step", "wrong_id"])
def test_native_observer_errors_restore_every_hook_and_method(failure):
    model = Model(fail=failure == "compute")
    target = worker(model)
    trace.arm(target, "a" * 32, [1, 7], [0, 3])
    inputs = torch.arange(16, dtype=torch.float32).reshape(1, 16)
    if failure == "nonfinite":
        inputs[0, 0] = float("nan")
    with pytest.raises((ValueError, RuntimeError)):
        if failure == "wrong_id":
            trace.collect(target, "b" * 32)
        else:
            model(inputs)
            if failure == "second_step":
                model(inputs)
    assert "compute_logits" not in model.__dict__
    assert not hasattr(target, "_ws_readonly_trace")
    assert all(len(layer._forward_hooks) == 0 for layer in model.layers)


def test_complete_no_drift_matrix_is_readonly_and_snapshot_bound(tmp_path, monkeypatch):
    events = []

    class Fake:
        def __init__(self, *args):
            self.generation_count = 0
            self.comparisons = []
            self.traces = []

        def rpc(self, method, args):
            events.append(method)
            assert method == "ws_audit_runtime_info"
            return [{"rank": i, "version": 0} for i in range(8)]

        def snapshot(self, label):
            events.append(label)
            return [{"rank": i, "pid": i} for i in range(8)]

        def sample(self, label, prompt, count=1):
            self.generation_count += 1
            events.append((label, list(prompt), count))
            return response(prompt, ids=(7,) * count)["choices"][0]

        def compare(self, label, before, after, context):
            result = probe.difference(before, after)
            self.comparisons.append(result)
            return result

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"text_config": {"num_hidden_layers": 4}}))
    args = SimpleNamespace(
        model_config=config,
        expected_config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
    )
    monkeypatch.setenv("AMD_EXPERIMENT_SERVER_URL", "http://unused.invalid")
    monkeypatch.setattr(probe, "ReadOnlyExperiment", Fake)
    monkeypatch.setattr(probe.proof, "expected_runtime_profile", lambda: {})
    monkeypatch.setattr(probe.proof, "validate_batch_records", lambda *args: None)
    assert probe.run(args, tmp_path) == 0
    assert events[1] == "A-before" and events[-1] == "A-after"
    scope = json.loads((tmp_path / "scope.json").read_bytes())
    assert scope["generation_responses"] == 50
    assert (
        scope["full_smoke_accepted"]
        is scope["sparse_writes_requested"]
        is scope["whole_model_immutability_proven"]
        is False
    )
    assert scope["selected_parameter_snapshots_exact"] is True


def test_actual_transport_retains_full_raw_response_and_token_ids(
    tmp_path, monkeypatch
):
    import io

    raw = json.dumps(response(), indent=3).encode()
    monkeypatch.setenv("AMD_EXPERIMENT_MODEL", "frozen-model")
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *args, **kwargs: io.BytesIO(raw)
    )
    experiment = probe.ReadOnlyExperiment(tmp_path, "http://unused.invalid")
    choice = experiment.sample("paired-a", [4, 5, 6])
    assert choice["token_ids"] == [7]
    assert (tmp_path / "http-001-raw.json").read_bytes() == raw
    saved = json.loads((tmp_path / "paired-a.json").read_bytes())
    assert saved["response"] == json.loads(raw)
    assert saved["raw_response_sha256"] == hashlib.sha256(raw).hexdigest()


def test_readonly_response_cap_fails_before_unbounded_json_decode(
    tmp_path, monkeypatch
):
    import io

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *args, **kwargs: io.BytesIO(b" " * 65537)
    )
    experiment = probe.ReadOnlyExperiment(tmp_path, "http://unused.invalid")
    with pytest.raises(ValueError, match="65536bytes"):
        experiment.request("/v1/completions", {})
    assert (tmp_path / "rpc-001-error.json").exists()
    assert not (tmp_path / "http-001-raw.json").exists()


def test_trace_host_finally_restores_on_generation_failure(tmp_path):
    experiment = probe.ReadOnlyExperiment(tmp_path, "http://unused.invalid")
    baseline = [{"rank": i, "pid": i + 1} for i in range(8)]
    calls = []

    def rpc(method, args):
        calls.append(method)
        return [
            dict(r, armed=True, trace_id=args[0]) if args else dict(r, cancelled=True)
            for r in baseline
        ]

    experiment.rpc = rpc
    experiment.sample = lambda *args: (_ for _ in ()).throw(
        TimeoutError("actual request failure")
    )
    with pytest.raises(TimeoutError):
        experiment.trace_once("failed", [4], [1], [0], baseline)
    assert calls == ["ws_localization_arm", "ws_localization_cancel"]


def test_missing_collective_rank_cannot_be_a_passing_trace():
    with pytest.raises(ValueError, match="eight distinct"):
        probe.validate_trace_ranks([{"rank": 0, "pid": 1}], [{"rank": 0, "pid": 1}])


def test_drift_progressively_traces_actual_mismatched_ids(tmp_path, monkeypatch):
    traced = []

    class Fake:
        def __init__(self, *args):
            self.generation_count = 0
            self.comparisons = []
            self.traces = []

        def rpc(self, method, args):
            assert method == "ws_audit_runtime_info"
            return [{"rank": i, "version": 0} for i in range(8)]

        def snapshot(self, label):
            return [{"rank": i, "pid": i + 1} for i in range(8)]

        def sample(self, label, prompt, count=1):
            self.generation_count += 1
            return response(prompt, ids=(77 if label.endswith("-a") else 88,) * count)[
                "choices"
            ][0]

        def compare(self, label, before, after, context):
            result = probe.difference(before, after)
            self.comparisons.append(result)
            return result

        def trace_once(self, label, context, ids, layers, baseline):
            assert ids == [77, 88]
            assert context == list(probe.PROMPTS[0])
            traced.append(layers)
            records = [
                dict(
                    r,
                    records=[
                        {
                            "boundary": "layer",
                            "layer": layer,
                            "sha256": "changed"
                            if label.endswith("-b") and layer >= 65
                            else "same",
                        }
                        for layer in layers
                    ],
                )
                for r in baseline
            ]
            self.traces.append(records)
            return self.sample(label, context), records

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"text_config": {"num_hidden_layers": 93}}))
    args = SimpleNamespace(
        model_config=config,
        expected_config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
    )
    monkeypatch.setenv("AMD_EXPERIMENT_SERVER_URL", "http://unused.invalid")
    monkeypatch.setattr(probe, "ReadOnlyExperiment", Fake)
    monkeypatch.setattr(probe.proof, "expected_runtime_profile", lambda: {})
    monkeypatch.setattr(probe.proof, "validate_batch_records", lambda *args: None)
    assert probe.run(args, tmp_path) == 2
    assert (
        traced
        == [[0, 30, 61, 92]] * 2
        + [[62, 72, 82, 92]] * 2
        + [[63, 66, 69, 72]] * 2
        + [[64, 65, 66]] * 2
    )
    scope = json.loads((tmp_path / "scope.json").read_bytes())
    assert scope["generation_responses"] == 58
    assert scope["trace_pairs"] == 4 and scope["drift_observed"] is True
    assert scope["full_smoke_accepted"] is False


def test_layer_trace_hash_includes_residual_component():
    class ResidualLayer(torch.nn.Module):
        def forward(self, value):
            return value, value + 1

    model = Model()
    model.layers[0] = ResidualLayer()
    target = worker(model)
    trace.arm(target, "a" * 32, [1], [0])
    model.layers[0](torch.zeros((1, 16)))
    model.compute_logits(torch.zeros((1, 16)))
    captured = trace.collect(target, "a" * 32)
    layer = next(r for r in captured["records"] if r["boundary"] == "layer")
    assert len(layer["components"]) == 2
    assert layer["components"][0]["sha256"] != layer["components"][1]["sha256"]


@pytest.mark.parametrize("damage", [None, "archive", "startup", "conflict"])
def test_offline_prepare_freezes_exact_readonly_contract(tmp_path, monkeypatch, damage):
    import io
    import sys
    import tarfile
    from scripts.vllm_drift import prepare

    base = tmp_path / "base"
    base.mkdir()
    startup = tmp_path / "startup.json"
    startup.write_text(
        json.dumps({"model_path": "/frozen/model", "model_config_sha256": "a" * 64})
    )

    def digest(raw):
        return hashlib.sha256(raw).hexdigest()

    files = {
        "expected-runtime-profile.json": json.dumps(
            {
                "batch_invariant": damage == "startup",
                "startup_sha256": digest(startup.read_bytes()),
            }
        ).encode(),
        "native-source-manifest.json": json.dumps({"vllm/a.py": "b" * 64}).encode(),
        "expected-model-config.json": b"{}",
        "expected-execution-contract.json": b"{}",
        "overlay-manifest.json": b"{}",
    }
    with tarfile.open(base / "overlay.tar.gz", "w:gz") as archive:
        for name, raw in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    plan = {
        "overlay_sha256": digest((base / "overlay.tar.gz").read_bytes()),
        "files": {p: digest(raw) for p, raw in files.items()},
    }
    (base / "plan.json").write_text(json.dumps(plan))
    if damage == "archive":
        (base / "overlay.tar.gz").write_bytes(b"changed")
    extra = tmp_path / "extra.json"
    extra.write_text(
        json.dumps(
            {"vllm/a.py": "c" * 64} if damage == "conflict" else {"vllm/b.py": "c" * 64}
        )
    )
    out = tmp_path / "out"
    argv = [
        "prepare",
        "--base-candidate",
        str(base),
        "--extra-source-manifest",
        str(extra),
        "--startup-profile",
        str(startup),
        "--out",
        str(out),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    if damage:
        with pytest.raises(ValueError):
            prepare.main()
        assert not out.exists()
        return
    prepare.main()
    actual = json.loads((out / "plan.json").read_bytes())
    assert actual["native_source_count"] == 2
    assert actual["resources_claimed"] is False
    contract = actual["execution_contract"]
    assert contract["harness_argv"][1] == "scripts.vllm_drift.probe"
    assert contract["effective_validator_argv"][-1] == "--validate-runtime-only"
    assert contract["forbidden_mutation_rpc"] is True
    assert contract["bounds"]["max_generation_responses"] == 58
    assert contract["full_smoke_accepted"] is False
    with tarfile.open(out / "overlay.tar.gz") as archive:
        actual_files = {
            m.name: archive.extractfile(m).read() for m in archive.getmembers()
        }
    assert actual["files"] == {p: digest(raw) for p, raw in actual_files.items()}
    assert "scripts/vllm_drift/prepare.py" not in actual_files
    assert actual_files["expected-model-config.json"] == b"{}"
    argv[-1] = str(tmp_path / "out-again")
    prepare.main()
    assert (out / "overlay.tar.gz").read_bytes() == (
        tmp_path / "out-again" / "overlay.tar.gz"
    ).read_bytes()
