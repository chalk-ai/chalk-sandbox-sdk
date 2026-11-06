"""CPU orchestration tests; fake worker receipts do not establish native correctness."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts.vllm_sparse import pod_script as pod
from scripts.vllm_sparse import summarize_result


def output():
    return [
        {
            "text": "a",
            "logprobs": {
                "tokens": ["a"],
                "token_logprobs": [-1.0],
                "top_logprobs": [{"a": -1.0}],
            },
        }
        for _ in range(8)
    ]


def retain(directory, label, values):
    pod.write(directory / (label + "-generation.json"), values)
    records = []
    for index, value in enumerate(values):
        name = f"http-{label}-{index}-raw.json"
        raw = json.dumps({"choices": [value]}).encode()
        (directory / name).write_bytes(raw)
        records.append({"path": name, "sha256": hashlib.sha256(raw).hexdigest()})
    pod.write(
        directory / (label + "-generation-bindings.json"),
        {
            "output_sha256": hashlib.sha256(
                (directory / (label + "-generation.json")).read_bytes()
            ).hexdigest(),
            "raw_responses": records,
        },
    )


@pytest.mark.parametrize("difference", ["text", "tokens", "keys", "score"])
def test_only_valid_comparison_differences_are_recorded(tmp_path, difference):
    before, after = output(), output()
    if difference == "text":
        after[0]["text"] = "b"
    elif difference == "tokens":
        after[0]["logprobs"]["tokens"] = ["b"]
    elif difference == "keys":
        after[0]["logprobs"]["top_logprobs"] = [{"b": -1.0}]
    else:
        after[0]["logprobs"]["token_logprobs"] = [-1.002]
    retain(tmp_path, "before", before)
    retain(tmp_path, "after", after)
    comparisons = pod.DiagnosticComparisons(tmp_path)
    record = comparisons.compare("before", before, "after", after)
    assert record["passed"] is False
    assert comparisons.failed
    assert set(record["bindings"]) == {"before", "after"}
    assert json.loads(
        (tmp_path / "diagnostic-generation-comparisons.json").read_text()
    ) == [record]
    with pytest.raises(ValueError):
        pod.compare_generation(before, after)


@pytest.mark.parametrize(
    "damage",
    [
        "nan_hidden_after_text",
        "inf",
        "bool",
        "missing",
        "short_scores",
        "empty_top",
        "short_prompts",
        "empty_tokens",
    ],
)
def test_malformed_or_nonfinite_output_is_fatal_even_after_earlier_difference(
    tmp_path, damage
):
    before, after = output(), output()
    after[0]["text"] = "different"
    logprobs = after[-1]["logprobs"]
    if damage == "nan_hidden_after_text":
        logprobs["top_logprobs"][0]["a"] = float("nan")
    elif damage == "inf":
        logprobs["token_logprobs"][0] = float("inf")
    elif damage == "bool":
        logprobs["token_logprobs"][0] = True
    elif damage == "missing":
        del after[-1]["text"]
    elif damage == "short_scores":
        logprobs["token_logprobs"] = []
    elif damage == "empty_top":
        logprobs["top_logprobs"] = [{}]
    elif damage == "short_prompts":
        after.pop()
    else:
        logprobs["tokens"] = []
    comparisons = pod.DiagnosticComparisons(tmp_path)
    with pytest.raises(ValueError):
        comparisons.compare("before", before, "after", after)
    assert comparisons.records == []


def test_raw_and_output_bindings_fail_closed(tmp_path):
    values = output()
    retain(tmp_path, "before", values)
    retain(tmp_path, "after", values)
    (tmp_path / "http-after-7-raw.json").write_text("{}")
    with pytest.raises(ValueError, match="raw response binding"):
        pod.DiagnosticComparisons(tmp_path).compare("before", values, "after", values)


def setup_run(monkeypatch, tmp_path, *, diagnostic=True, fail=None, differences=True):
    events = []
    config = tmp_path / "config.json"
    config.write_bytes(b"{}")
    directory = tmp_path / "run"
    directory.mkdir()
    baseline = [
        {
            "rank": rank,
            "pid": rank + 1,
            "device": rank,
            "model_id": 99,
            "process_start_ticks": "42",
        }
        for rank in range(8)
    ]
    runtime = [dict(record, version=0, pid_start_ticks=42) for record in baseline]

    class FakeExperiment:
        def __init__(self, directory, url, **kwargs):
            self.directory = directory

        def rpc(self, method, args):
            events.append(("rpc", method))
            if method == "ws_audit_runtime_info":
                return runtime
            assert method == "ws_audit_commit"
            return [
                {"idempotent_no_write_replay": True, "sparse_writes": 0}
                for _ in range(8)
            ]

        def snapshot(self, label):
            events.append(("snapshot", label))
            if fail == "snapshot" and label == "A-postgeneration":
                return []
            return copy.deepcopy(baseline)

        def generate(self, label):
            events.append(("generation", label))
            if fail == "transport" and label == "A-repeat":
                raise TimeoutError("actual transport-style failure")
            values = output()
            if differences and label in (
                "A-repeat",
                "replay_B",
                "inverse_A",
                "corrupt_refusal",
            ):
                values[0]["text"] = "different"
            if fail == "schema" and label == "A-repeat":
                values[-1]["logprobs"]["token_logprobs"] = []
            retain(directory, label, values)
            return values

        def apply(self, root, version):
            events.append(("apply", root.name))
            return "transaction", {}, "digest"

    def build(root, *args):
        root.mkdir()
        (root / "patch-0.bin").write_bytes(b"123")
        return {}

    def check_bytes(*args):
        events.append(("bytes", "checked"))
        if fail == "bytes":
            raise ValueError("byte violation")

    monkeypatch.setattr(
        pod.summarize_result,
        "verify_byte_control_receipts",
        lambda *args: {"passed": True},
    )
    monkeypatch.setattr(pod, "validate_diagnostic_commit", lambda *args, **kwargs: None)
    monkeypatch.setattr(pod, "Experiment", FakeExperiment)
    monkeypatch.setattr(pod.proof, "selected_architecture_hash", lambda _: "arch")
    monkeypatch.setattr(pod.proof, "expected_runtime_profile", lambda: {})
    monkeypatch.setattr(pod.proof, "validate_batch_records", lambda *args: None)
    monkeypatch.setattr(pod, "samples", lambda _: {})
    monkeypatch.setattr(pod, "build", build)
    monkeypatch.setattr(pod, "check_bytes", check_bytes)
    monkeypatch.setattr(
        pod, "corrupt_refusal", lambda *args: events.append(("refusal", "checked"))
    )
    monkeypatch.setenv("AMD_EXPERIMENT_SERVER_URL", "http://unused.invalid")
    args = SimpleNamespace(
        model_config=config,
        expected_config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        validate_runtime_only=False,
    )
    if diagnostic:
        args.diagnostic_mode = pod.DIAGNOSTIC_MODE
    return args, directory, events


def test_diagnostic_runs_all_mandatory_cases_but_exits_two_and_never_smoke_passes(
    monkeypatch, tmp_path
):
    args, directory, events = setup_run(monkeypatch, tmp_path)
    assert pod.run_experiment(args, directory) == 2
    generation_labels = [value for kind, value in events if kind == "generation"]
    assert generation_labels == [
        "A-cold",
        "A-repeat",
        "B",
        "replay_B",
        "C",
        "replay_C",
        "inverse_A",
        "replay_inverse_A",
        "corrupt_refusal",
    ]
    assert events.index(("snapshot", "A-postgeneration")) < events.index(("apply", "B"))
    assert ("snapshot", "corruption-refusal-postgeneration") in events
    scope = json.loads((directory / "scope.json").read_text())
    assert scope["status"] == "diagnostic_complete"
    assert scope["full_smoke_accepted"] is False
    assert scope["byte_control_status"] == "passed"
    assert scope["mandatory_cases"] == pod.DIAGNOSTIC_CASES
    assert scope["generation_comparison_failures"] == 5
    assert summarize_result.verify_sparse(directory)["passed"] is False
    comparisons = json.loads(
        (directory / "diagnostic-generation-comparisons.json").read_text()
    )
    assert len(comparisons) == 6
    assert sum(not record["passed"] for record in comparisons) == 5


@pytest.mark.parametrize("failure", ["schema", "transport", "snapshot", "bytes"])
def test_fatal_gates_never_report_completed(monkeypatch, tmp_path, failure):
    args, directory, events = setup_run(monkeypatch, tmp_path, fail=failure)
    with pytest.raises((ValueError, TimeoutError)):
        pod.run_experiment(args, directory)
    assert json.loads((directory / "scope.json").read_text())["status"] == "running"
    if failure != "bytes":
        assert not any(kind == "apply" for kind, _ in events)
    else:
        assert [value for kind, value in events if kind == "apply"] == ["B"]


def test_default_strict_mode_still_stops_at_unchanged_baseline(monkeypatch, tmp_path):
    args, directory, events = setup_run(monkeypatch, tmp_path, diagnostic=False)
    with pytest.raises(ValueError, match="generation tokens differ"):
        pod.run_experiment(args, directory)
    assert not any(kind == "apply" for kind, _ in events)
    assert ("snapshot", "A-postgeneration") not in events
    assert not (directory / "diagnostic-generation-comparisons.json").exists()


def test_main_preserves_exit_two_as_completed_diagnostic(monkeypatch, tmp_path):
    monkeypatch.setenv("AMD_EXPERIMENT_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sys.argv",
        [
            "pod_script",
            "--execute-reviewed",
            "--diagnostic-mode",
            pod.DIAGNOSTIC_MODE,
            "--model-config",
            "unused",
            "--expected-config-sha256",
            "unused",
        ],
    )

    def run(args, directory):
        pod.write(
            directory / "scope.json",
            {"status": "diagnostic_complete", "full_smoke_accepted": False},
        )
        return 2

    monkeypatch.setattr(pod, "run_experiment", run)
    with pytest.raises(SystemExit) as error:
        pod.main()
    assert error.value.code == 2
    assert not (tmp_path / "sparse-proof/failure.json").exists()


def test_diagnostic_cannot_skip_matrix_for_runtime_only(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "pod_script",
            "--execute-reviewed",
            "--validate-runtime-only",
            "--diagnostic-mode",
            pod.DIAGNOSTIC_MODE,
            "--model-config",
            "unused",
            "--expected-config-sha256",
            "unused",
        ],
    )
    with pytest.raises(SystemExit) as error:
        pod.main()
    assert error.value.code == 2


def test_fully_matching_diagnostic_still_cannot_accept_full_smoke(
    monkeypatch, tmp_path
):
    args, directory, events = setup_run(monkeypatch, tmp_path, differences=False)
    assert pod.run_experiment(args, directory) == 0
    scope = json.loads((directory / "scope.json").read_text())
    assert scope["status"] == "diagnostic_complete"
    assert scope["generation_comparison_failures"] == 0
    assert scope["full_smoke_accepted"] is False
    assert summarize_result.verify_sparse(directory)["passed"] is False


def test_exported_byte_control_failure_cannot_complete_diagnostic(
    monkeypatch, tmp_path
):
    args, directory, events = setup_run(monkeypatch, tmp_path)

    def refuse(*args):
        raise ValueError("missing all-rank actual index_copy receipt")

    monkeypatch.setattr(pod.summarize_result, "verify_byte_control_receipts", refuse)
    with pytest.raises(ValueError, match="index_copy"):
        pod.run_experiment(args, directory)
    assert json.loads((directory / "scope.json").read_text())["status"] == "running"


def test_real_http_raw_bytes_are_retained_and_bound(monkeypatch, tmp_path):
    import io

    raw = json.dumps({"choices": [output()[0]]}, indent=3).encode()
    monkeypatch.setenv("AMD_EXPERIMENT_MODEL", "fake-cpu-only")
    monkeypatch.setattr(
        pod.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(raw)
    )
    experiment = pod.Experiment(tmp_path, "http://unused.invalid", diagnostic=True)
    first = experiment.generate("first")
    second = experiment.generate("second")
    assert (tmp_path / "http-001-raw.json").read_bytes() == raw
    record = pod.DiagnosticComparisons(tmp_path).compare(
        "first", first, "second", second
    )
    assert record["passed"] is True
    assert (
        record["bindings"]["first"]["raw_responses"][0]["sha256"]
        == hashlib.sha256(raw).hexdigest()
    )
    # Raw evidence must not look like a canonical collective-RPC receipt.
    assert not list(tmp_path.glob("rpc-*-raw.json"))


@pytest.mark.parametrize(
    "response", [{}, {"choices": []}, {"choices": [output()[0], output()[0]]}]
)
def test_invalid_completion_envelope_is_fatal(tmp_path, response):
    experiment = pod.Experiment(tmp_path, "http://unused.invalid", diagnostic=True)
    with pytest.raises(ValueError, match="completion response schema"):
        experiment.completion_choice(response)


def test_unrecognized_comparator_errors_remain_fatal(monkeypatch, tmp_path):
    values = output()
    retain(tmp_path, "before", values)
    retain(tmp_path, "after", values)

    def unknown(*args):
        raise ValueError("future unexpected invariant failure")

    monkeypatch.setattr(pod, "compare_generation", unknown)
    comparisons = pod.DiagnosticComparisons(tmp_path)
    with pytest.raises(ValueError, match="unexpected invariant"):
        comparisons.compare("before", values, "after", values)
    assert comparisons.records == []


def test_early_diagnostic_failure_always_marks_full_smoke_unaccepted(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AMD_EXPERIMENT_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sys.argv",
        [
            "pod_script",
            "--execute-reviewed",
            "--diagnostic-mode",
            pod.DIAGNOSTIC_MODE,
            "--model-config",
            "unused",
            "--expected-config-sha256",
            "unused",
        ],
    )

    def fail(*args):
        raise TimeoutError("before initial scope")

    monkeypatch.setattr(pod, "run_experiment", fail)
    with pytest.raises(TimeoutError):
        pod.main()
    scope = json.loads((tmp_path / "sparse-proof/scope.json").read_text())
    assert scope["status"] == "failed"
    assert scope["mode"] == pod.DIAGNOSTIC_MODE
    assert scope["full_smoke_accepted"] is False
