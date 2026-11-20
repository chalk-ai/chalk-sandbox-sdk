"""Exact reviewed ROCm release packaging policy; no image or GPU required."""

import json
import sys

import pytest

from scripts.vllm_sparse import runtime_gate


@pytest.mark.parametrize("version", ["0.33.0", "0.33.0+rocm100"])
def test_exact_reviewed_release_variants(version):
    runtime_gate.require_reviewed_versions(
        {"python": "3.12.9", "package_versions": {"vllm": version}}
    )


@pytest.mark.parametrize(
    "version",
    [
        "0.33",
        "0.33.1",
        "0.34.0",
        "0.33.0.dev1",
        "0.33.0.dev1+rocm100",
        "0.33.0+rocm72",
        "0.33.0+rocm100.extra",
        "0.33.0+cu130",
        "0.33.0+cpu",
        "0.33.0+precompiled",
        "0.33.0+ROCM100",
        "v0.33.0",
    ],
)
def test_other_release_development_and_local_variants_rejected(version):
    with pytest.raises(ValueError, match="release packaging"):
        runtime_gate.require_reviewed_versions(
            {"python": "3.12.9", "package_versions": {"vllm": version}}
        )


def test_python_minor_gate_is_unchanged():
    with pytest.raises(ValueError, match="Python3.12"):
        runtime_gate.require_reviewed_versions(
            {"python": "3.13.1", "package_versions": {"vllm": "0.33.0+rocm100"}}
        )


@pytest.mark.parametrize("version", runtime_gate.REVIEWED_VLLM_VERSIONS)
@pytest.mark.parametrize("failure", ["source", "dependency"])
def test_allowed_packaging_cannot_bypass_source_or_dependency_attestation(
    tmp_path, monkeypatch, version, failure
):
    receipt = {
        "python": "3.12.9",
        "package_versions": {"vllm": version},
        "sources": {"vllm/models/kimi.py": {"match": failure != "source"}},
        "errors": ["Missing required installed package:xxhash"]
        if failure == "dependency"
        else [],
    }
    monkeypatch.setattr(runtime_gate, "collect", lambda *_: receipt)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    out = tmp_path / "gate.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runtime_gate",
            "--source-manifest",
            str(manifest),
            "--overlay-manifest",
            str(manifest),
            "--out",
            str(out),
            "--inspect-only",
        ],
    )
    with pytest.raises(ValueError, match="attestation gate"):
        runtime_gate.main()
    observed = json.loads(out.read_text())
    assert observed["passed"] is False
    assert observed["package_versions"]["vllm"] == version
    assert "cpu_sparse_roundtrip" not in observed
