from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tarfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "sglang_preflight_payload",
    Path(__file__).parents[1] / "scripts" / "sglang" / "preflight_payload.py",
)
assert spec and spec.loader
payload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(payload)

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts" / "sglang"))
runner_spec = importlib.util.spec_from_file_location(
    "sglang_cpu_preflight",
    Path(__file__).parents[1] / "scripts" / "sglang" / "cpu_preflight.py",
)
assert runner_spec and runner_spec.loader
runner = importlib.util.module_from_spec(runner_spec)
runner_spec.loader.exec_module(runner)


def test_cpu_cuda_stack_contract():
    payload.validate_versions(
        {"sglang": "0.5.23", "torch": "2.14.1+cu130", "transformers": "5.19.0"},
        cuda="13.0",
        hip=None,
    )


@pytest.mark.parametrize(
    "versions,cuda,hip",
    [
        (
            {"sglang": "0.5.24", "torch": "2.14.1", "transformers": "5.19.0"},
            "13.0",
            None,
        ),
        (
            {"sglang": "0.5.23", "torch": "2.14.0", "transformers": "5.19.0"},
            "13.0",
            None,
        ),
        (
            {"sglang": "0.5.23", "torch": "2.14.1", "transformers": "5.18.0"},
            "13.0",
            None,
        ),
        (
            {"sglang": "0.5.23", "torch": "2.14.1", "transformers": "5.19.0"},
            "12.8",
            None,
        ),
        (
            {"sglang": "0.5.23", "torch": "2.14.1", "transformers": "5.19.0"},
            None,
            "7.2",
        ),
        (
            {"sglang": "0.5.23", "torch": "2.14.1", "transformers": "5.19.0"},
            "13.0",
            "7.2",
        ),
    ],
)
def test_unknown_or_rocm_stack_rejected(versions, cuda, hip):
    with pytest.raises(ValueError):
        payload.validate_versions(versions, cuda=cuda, hip=hip)


def test_every_native_control_and_loader_source_must_match(tmp_path):
    expected = {}
    for relative in payload.SOURCE_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
        expected[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert payload.verify_sources(tmp_path, expected) == expected
    with pytest.raises(ValueError, match="every"):
        payload.verify_sources(tmp_path, {})
    (tmp_path / next(iter(expected))).write_text("different source")
    with pytest.raises(ValueError, match="differs"):
        payload.verify_sources(tmp_path, expected)


def test_source_symlink_escape_rejected(tmp_path):
    package = tmp_path / "sglang"
    package.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (package / "srt").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        payload.verify_sources(package, {key: "a" * 64 for key in payload.SOURCE_FILES})


@pytest.fixture
def native_schemas(tmp_path):
    io = tmp_path / "srt/managers/io_struct.py"
    io.parent.mkdir(parents=True)
    io.write_text("""
class PauseGenerationReqInput(BaseReq, kw_only=True):
    mode: Literal["abort", "retract", "in_place"] = "abort"
class ContinueGenerationReqInput(BaseReq, kw_only=True):
    torch_empty_cache: bool = True
class UpdateWeightFromDiskReqInput(BaseReq, kw_only=True):
    model_path: str
    load_format: Optional[str] = None
    abort_all_requests: bool = False
    weight_version: Optional[str] = None
    is_async: bool = False
    torch_empty_cache: bool = False
    keep_pause: bool = False
    recapture_cuda_graph: bool = False
    token_step: int = 0
    flush_cache: bool = True
    manifest: Optional[Dict[str, Any]] = None
""")
    protocol = tmp_path / "srt/entrypoints/openai/protocol.py"
    protocol.parent.mkdir(parents=True)
    protocol.write_text("""
class CompletionRequest(PDRoutingFields):
    ignore_eos: bool = False
""")
    return tmp_path, io, protocol


def test_native_schema_proof_requires_no_native_imports(native_schemas):
    root, _, _ = native_schemas
    proof = payload.verify_native_schemas(root)
    assert proof["fields"]["CompletionRequest.ignore_eos"] == {
        "annotation": "bool",
        "default": "False",
    }
    assert proof["native_cli_help_executed"] is False
    assert proof["native_request_parsing_executed"] is False


@pytest.mark.parametrize(
    "before,after",
    [
        ('"in_place"', '"unsupported"'),
        ("keep_pause: bool = False", "keep_pause: bool = True"),
        ("is_async: bool = False", "is_async: str = False"),
        ("model_path: str", "other_path: str"),
        ("flush_cache: bool = True", "flush_cache: bool = False"),
    ],
)
def test_incompatible_native_control_schema_rejected(native_schemas, before, after):
    root, io, _ = native_schemas
    io.write_text(io.read_text().replace(before, after))
    with pytest.raises(ValueError, match="Native request schema differs"):
        payload.verify_native_schemas(root)


@pytest.mark.parametrize(
    "declaration", ["pass", "ignore_eos: str = False", "ignore_eos: bool = True"]
)
def test_openai_ignore_eos_must_have_audited_type_and_default(
    native_schemas, declaration
):
    root, _, protocol = native_schemas
    protocol.write_text(
        "class CompletionRequest(PDRoutingFields):\n    " + declaration + "\n"
    )
    with pytest.raises(ValueError, match="CompletionRequest.ignore_eos"):
        payload.verify_native_schemas(root)


def test_bundle_is_small_self_contained_and_hash_bound(tmp_path):
    root = tmp_path / "fixture"
    root.mkdir()
    (root / "config.json").write_text("{}")
    output = tmp_path / "fixture.tar.gz"
    proof = payload.bundle_fixture(root, output)
    assert proof["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert proof["bytes"] == output.stat().st_size
    with tarfile.open(output) as archive:
        assert archive.getnames() == ["config.json"]
    with pytest.raises(ValueError, match="new file"):
        payload.bundle_fixture(root, output)


def test_bundle_rejects_symlinks_and_oversize(tmp_path, monkeypatch):
    root = tmp_path / "fixture"
    root.mkdir()
    (root / "link").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="ordinary"):
        payload.bundle_fixture(root, tmp_path / "first.tar.gz")
    (root / "link").unlink()
    (root / "large").write_bytes(b"12345")
    monkeypatch.setattr(payload, "MAX_BUNDLE_BYTES", 4)
    with pytest.raises(ValueError, match="Uncompressed"):
        payload.bundle_fixture(root, tmp_path / "second.tar.gz")


def test_bundle_cannot_include_itself(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        payload.bundle_fixture(tmp_path, tmp_path / "bundle.tar.gz")


@pytest.mark.parametrize(
    "changes", [[0.1, 0.01], [0.0, 0.2], [0.1, float("nan")], [0.1]]
)
def test_both_fixture_updates_must_exceed_native_tolerance(changes):
    with pytest.raises(ValueError, match="Every fixture update"):
        payload.validate_fixture_changes(changes)
    payload.validate_fixture_changes([0.02, 0.2])


@pytest.fixture
def prepared(tmp_path):
    source = tmp_path / "sdk"
    package = source / "chalkcompute"
    package.mkdir(parents=True)
    for name in ("__init__.py", "_weights_codec.py", "_weights_format.py"):
        (package / name).write_text("# serving source\n")
    serving = package / "weights" / "serving"
    serving.mkdir(parents=True)
    (serving / "__init__.py").write_text("# runtime\n")
    guard = tmp_path / "guards"
    guard.mkdir()
    for name in runner.GUARD_FILES:
        (guard / name).write_text("# reviewed lifecycle\n")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    for name in payload.SOURCE_FILES.values():
        (upstream / name).write_text("# audited upstream\n")
    output = tmp_path / "prepared"
    plan = runner.prepare(output, source, guard, upstream)
    return output, plan


def test_preparation_freezes_only_cpu_plan_without_resource_apis(prepared):
    output, plan = prepared
    assert runner.validate_prepared(output) == plan
    assert plan["lease_seconds"] == 900 and plan["gpu_claimed"] is False
    assert plan["owned_volume_names"] == [] and plan["compute_class"] == "K8S"
    assert plan["chalk_identity"] is False
    assert not (output / "launcher.json").exists()


@pytest.mark.parametrize(
    "name,value",
    [
        ("gpu_claimed", True),
        ("chalk_identity", True),
        ("lease_seconds", 901),
        ("cpu", "8"),
        ("memory", "32Gi"),
        ("compute_class", "HOST"),
        ("owned_volume_names", ["someone-elses-volume"]),
        ("image", "lmsysorg/sglang:latest"),
    ],
)
def test_resource_or_image_plan_tamper_rejected_before_claim(prepared, name, value):
    output, plan = prepared
    plan[name] = value
    (output / "plan.json").write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="resource/image"):
        runner.validate_prepared(output)
    assert not (output / "execution.lock").exists()


@pytest.mark.parametrize(
    "name",
    ["overlay.tar.gz", "preflight_payload.py", "cpu_preflight.py", "guard/cpu_seed.py"],
)
def test_frozen_payload_or_guard_tamper_rejected(prepared, name):
    output, _ = prepared
    (output / name).write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        runner.validate_prepared(output)


def test_overlay_excludes_native_extensions_and_training_code(prepared):
    output, _ = prepared
    with tarfile.open(output / "overlay.tar.gz") as archive:
        assert sorted(archive.getnames()) == [
            "chalkcompute/__init__.py",
            "chalkcompute/_weights_codec.py",
            "chalkcompute/_weights_format.py",
            "chalkcompute/weights/serving/__init__.py",
        ]


def test_cpu_constructor_disables_default_managed_creator_identity():
    import chalkcompute as cc

    sandbox = runner.make_sandbox(cc, "unsubmitted-cpu-plan", payload.IMAGE, "pass")
    assert sandbox.id is None
    assert sandbox._chalk_identity is False


def test_host_constructor_is_cpu_only_identity_free_and_pool_scoped():
    import chalkcompute as cc

    sandbox = runner.make_sandbox(
        cc, "unsubmitted-host-plan", payload.IMAGE, "pass", host_prewarm=True
    )
    assert sandbox.id is None and sandbox._gpu is None
    assert sandbox._chalk_identity is False and sandbox._referenced_volumes == []
    assert sandbox._compute_class == cc.ComputeClass.HOST
    assert sandbox._runtime == cc.Runtime.GVISOR
    assert sandbox._host_pool_id == "ExternalClusterHosts"
    assert sandbox._cpu == "4" and sandbox._memory == "16Gi"


def test_host_preparation_freezes_extra_native_cleanup_guard(prepared):
    output, _ = prepared
    root = output.parent
    host_output = root / "host-prepared"
    plan = runner.prepare(
        host_output, root / "sdk", root / "guards", root / "upstream", host_prewarm=True
    )
    assert runner.validate_prepared(host_output) == plan
    assert plan["kind"] == "sglang_host_prewarm_plan" and plan["gpu_claimed"] is False
    assert plan["host_pool_id"] == "ExternalClusterHosts"
    assert plan["expected_host_id"] == "svlsbfe05ebc-oracle-a100"
    (host_output / "host_guard.py").write_text("changed")
    with pytest.raises(ValueError, match="HOST guard"):
        runner.validate_prepared(host_output)


def export_response(stdout="", *, error=None, exit_code=0, stderr=""):
    from types import SimpleNamespace

    return SimpleNamespace(
        stdout_text=stdout,
        error=error,
        exit_code=exit_code,
        stderr_text=stderr,
        signal=None,
    )


def run_export(tmp_path, responses, *, deadline=None):
    from types import SimpleNamespace
    import time

    calls = []

    def command(*args, **kwargs):
        calls.append((args, kwargs))
        value = next(responses)
        if isinstance(value, BaseException):
            raise value
        return value

    value = runner.export_guest_file(
        SimpleNamespace(exec=command),
        "/fixture.tar.gz",
        max_bytes=100000,
        record={"cpu_deadline_epoch": deadline or time.time() + 900},
        out=tmp_path,
    )
    return value, calls


def test_fixture_export_success_uses_bounded_responses(tmp_path):
    import base64

    data = b"x" * 70000
    responses = [export_response(str(len(data)))] + [
        export_response(base64.b64encode(data[start : start + 32768]).decode())
        for start in range(0, len(data), 32768)
    ]
    actual, calls = run_export(tmp_path, iter(responses))
    assert actual == data and len(calls) == 4
    assert calls[0][0][0] == "/opt/sglang/bin/python"
    assert not (tmp_path / "export-failure.json").exists()


@pytest.mark.parametrize(
    "failure",
    [
        export_response(
            error="token=private https://private.example/a",
            exit_code=1,
            stderr="Bearer sensitive",
        ),
        TimeoutError("transport timeout"),
    ],
)
def test_fixture_export_preserves_redacted_command_failure(tmp_path, failure):
    with pytest.raises((RuntimeError, TimeoutError)):
        run_export(tmp_path, iter([export_response("10"), failure]))
    diagnostic = json.loads((tmp_path / "export-failure.json").read_text())
    assert diagnostic["stage"] == "chunk" and diagnostic["offset"] == 0
    text = json.dumps(diagnostic)
    assert "sensitive" not in text and "private" not in text
    assert diagnostic["error_type"] in ("RuntimeError", "TimeoutError")


def test_fixture_export_rejects_short_chunk_and_retains_no_payload(tmp_path):
    with pytest.raises(RuntimeError, match="short"):
        run_export(tmp_path, iter([export_response("10"), export_response("YWJj")]))
    diagnostic = json.loads((tmp_path / "export-failure.json").read_text())
    assert diagnostic["stdout_bytes"] == 4
    assert "YWJj" not in json.dumps(diagnostic)


def test_fixture_export_stops_for_cleanup_reserve(tmp_path):
    import time

    with pytest.raises(TimeoutError, match="cleanup reserve"):
        run_export(tmp_path, iter([]), deadline=time.time() + 100)
    assert json.loads((tmp_path / "export-failure.json").read_text())["stage"] == "stat"
