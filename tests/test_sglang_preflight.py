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
    assert not (output / "launcher.json").exists()


@pytest.mark.parametrize(
    "name,value",
    [
        ("gpu_claimed", True),
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
