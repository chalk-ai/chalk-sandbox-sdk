from __future__ import annotations

import hashlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

from chalkcompute.weights.serving import resident_runtime as runtime


def test_exact_runtime_sources_required(tmp_path, monkeypatch):
    package = tmp_path / "vllm"
    package.mkdir()
    source = package / "model.py"
    source.write_bytes(b"audited source")
    monkeypatch.setattr(
        runtime.importlib.metadata,
        "version",
        lambda name: {
            "vllm": "0.33.0",
            **runtime.COMMON_PACKAGES,
            **runtime.RUNTIME_PACKAGES["vllm"],
        }[name],
    )
    monkeypatch.setattr(
        runtime.importlib.util,
        "find_spec",
        lambda _: SimpleNamespace(origin=str(package / "__init__.py")),
    )
    monkeypatch.setitem(
        runtime.RUNTIME_SOURCES,
        "vllm",
        {"model.py": hashlib.sha256(source.read_bytes()).hexdigest()},
    )
    runtime.check_runtime_sources("vllm")
    source.write_bytes(b"modified source")
    with pytest.raises(ValueError, match="Unreviewed"):
        runtime.check_runtime_sources("vllm")


@pytest.mark.parametrize("version", ["0.33.0+rocm100", "0.33.0.dev1", "0.34.0"])
def test_runtime_variant_cannot_bypass_admission(version, monkeypatch):
    monkeypatch.setattr(runtime.importlib.metadata, "version", lambda _: version)
    with pytest.raises(ValueError, match="requires"):
        runtime.check_runtime_sources("vllm")


def test_contract_digests_are_sha256():
    for sources in runtime.RUNTIME_SOURCES.values():
        for name, digest in sources.items():
            assert ".." not in name and not name.startswith("/")
            assert len(digest) == 64
            int(digest, 16)


@pytest.mark.parametrize("change", ["hip", "cpu", "multi", "no_bf16"])
def test_single_native_bf16_cuda_required(change, monkeypatch):
    from contextlib import nullcontext

    device = SimpleNamespace(type="cuda")
    torch = SimpleNamespace(
        device=lambda _: device,
        version=SimpleNamespace(cuda="13.0", hip=None),
        cuda=SimpleNamespace(
            device_count=lambda: 1,
            device=lambda _: nullcontext(),
            is_bf16_supported=lambda **kwargs: True,
        ),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    runtime.check_cuda("cuda:0")
    if change == "hip":
        torch.version.hip = "7.0"
    elif change == "cpu":
        device.type = "cpu"
    elif change == "multi":
        torch.cuda.device_count = lambda: 2
    else:
        torch.cuda.is_bf16_supported = lambda **kwargs: False
    with pytest.raises(ValueError, match="BF16"):
        runtime.check_cuda("cuda:0")


def test_resident_import_keeps_default_sdk_lightweight():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc, sys
class RejectHeavyImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'torch', 'vllm', 'sglang'} or fullname in {'chalkcompute._weights', 'chalkcompute._volume'}:
            raise AssertionError('Heavy import: ' + fullname)
sys.meta_path.insert(0, RejectHeavyImports())
import chalkcompute
import chalkcompute.weights.serving.resident
import chalkcompute.weights.serving.resident_runtime
""",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "package", ["torch", "transformers", "safetensors", "xxhash", "zstandard"]
)
def test_dependency_change_requires_review(package, monkeypatch):
    versions = {
        "vllm": "0.33.0",
        **runtime.COMMON_PACKAGES,
        **runtime.RUNTIME_PACKAGES["vllm"],
    }
    versions[package] = "unreviewed"
    monkeypatch.setattr(runtime.importlib.metadata, "version", versions.__getitem__)
    with pytest.raises(ValueError, match=f"requires {package}"):
        runtime.check_runtime_sources("vllm")
