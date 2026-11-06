"""Read-only source-bound batch-mode observations; no vLLM or device execution."""

import copy
import hashlib
from types import SimpleNamespace

import pytest

from scripts.vllm_sparse import audit_extension as audit
from scripts.vllm_sparse import proof

BATCH_PATH = "vllm/model_executor/determinism/batch_invariant.py"


def fixture_state(tmp_path, monkeypatch, enabled):
    source = tmp_path / "pinned_batch.py"
    source.write_bytes(b"source-bound observation fixture")
    profile = {
        "batch_invariant": enabled,
        "source_sha256": {BATCH_PATH: hashlib.sha256(source.read_bytes()).hexdigest()},
    }

    def must_not_initialize():
        pytest.fail("Audit must never initialize or enable batch mode")

    module = SimpleNamespace(
        __file__=str(source),
        _batch_invariant_MODE=enabled,
        _batch_invariant_LIB=object() if enabled else None,
        init_batch_invariance=must_not_initialize,
        enable_batch_invariant_mode=must_not_initialize,
    )
    envs = SimpleNamespace(VLLM_BATCH_INVARIANT=enabled)
    if enabled:
        monkeypatch.setenv("VLLM_BATCH_INVARIANT", "1")
    else:
        monkeypatch.delenv("VLLM_BATCH_INVARIANT", raising=False)
    matmul = SimpleNamespace(
        fp32_precision="ieee",
        allow_fp16_reduced_precision_reduction=(False, False),
        allow_bf16_reduced_precision_reduction=(False, False),
    )
    monkeypatch.setattr(
        audit,
        "torch",
        SimpleNamespace(
            backends=SimpleNamespace(
                cuda=SimpleNamespace(matmul=matmul),
                cudnn=SimpleNamespace(
                    conv=SimpleNamespace(fp32_precision="ieee"),
                    rnn=SimpleNamespace(fp32_precision="ieee"),
                ),
            )
        ),
    )
    return profile, module, envs


@pytest.mark.parametrize("enabled", [False, True])
def test_observes_frozen_profile_without_initializer_and_serializes_controls(
    tmp_path, monkeypatch, enabled
):
    profile, module, envs = fixture_state(tmp_path, monkeypatch, enabled)
    state = audit.batch_invariance_info(profile, batch_module=module, envs_module=envs)
    assert state["private_mode"] is state["private_library_present"] is enabled
    assert state["public_backend_controls"][
        "allow_fp16_reduced_precision_reduction"
    ] == [False, False]
    assert state["public_backend_controls"][
        "allow_bf16_reduced_precision_reduction"
    ] == [False, False]
    assert "private state" in state["scope"]
    audit.rpc_metadata(state)
    records = [
        {"rank": rank, "batch_invariance": copy.deepcopy(state)} for rank in range(8)
    ]
    proof.validate_batch_records(records, profile)


@pytest.mark.parametrize(
    "wrong", ["environment", "resolved", "mode", "library", "source"]
)
def test_enabled_expectation_rejects_missing_actual_state_or_wrong_source(
    tmp_path, monkeypatch, wrong
):
    profile, module, envs = fixture_state(tmp_path, monkeypatch, True)
    if wrong == "environment":
        monkeypatch.delenv("VLLM_BATCH_INVARIANT")
    elif wrong == "resolved":
        envs.VLLM_BATCH_INVARIANT = False
    elif wrong == "mode":
        module._batch_invariant_MODE = False
    elif wrong == "library":
        module._batch_invariant_LIB = None
    else:
        profile["source_sha256"][BATCH_PATH] = "0" * 64
    with pytest.raises(ValueError):
        audit.batch_invariance_info(profile, batch_module=module, envs_module=envs)


@pytest.mark.parametrize(
    "wrong", ["missing_rank", "duplicate_rank", "mode", "source", "environment"]
)
def test_host_requires_all_eight_expected_worker_observations(
    tmp_path, monkeypatch, wrong
):
    profile, module, envs = fixture_state(tmp_path, monkeypatch, True)
    state = audit.batch_invariance_info(profile, batch_module=module, envs_module=envs)
    records = [
        {"rank": rank, "batch_invariance": copy.deepcopy(state)} for rank in range(8)
    ]
    if wrong == "missing_rank":
        records.pop()
    elif wrong == "duplicate_rank":
        records[-1]["rank"] = 0
    else:
        key, value = {
            "mode": ("private_mode", False),
            "source": ("module_sha256", "0" * 64),
            "environment": ("environment_value", "0"),
        }[wrong]
        records[0]["batch_invariance"][key] = value
    with pytest.raises(ValueError):
        proof.validate_batch_records(records, profile)


def test_disabled_profile_never_imports_unloaded_invariant_module(
    tmp_path, monkeypatch
):
    profile, module, envs = fixture_state(tmp_path, monkeypatch, False)
    monkeypatch.delitem(
        audit.sys.modules,
        "vllm.model_executor.determinism.batch_invariant",
        raising=False,
    )

    def no_import(name):
        pytest.fail("Disabled audit attempted module import: " + name)

    monkeypatch.setattr(audit.importlib, "import_module", no_import)
    state = audit.batch_invariance_info(profile, envs_module=envs)
    assert state["module_loaded"] is False
    assert (
        state["private_mode"]
        is state["private_library_present"]
        is state["module_sha256"]
        is None
    )
    records = [
        {"rank": rank, "batch_invariance": copy.deepcopy(state)} for rank in range(8)
    ]
    proof.validate_batch_records(records, profile)
    records[-1]["batch_invariance"]["private_mode"] = False
    with pytest.raises(ValueError):
        proof.validate_batch_records(records, profile)


def test_disabled_profile_observes_existing_module_and_rejects_enabled_state(
    tmp_path, monkeypatch
):
    profile, module, envs = fixture_state(tmp_path, monkeypatch, False)
    monkeypatch.setitem(
        audit.sys.modules, "vllm.model_executor.determinism.batch_invariant", module
    )
    state = audit.batch_invariance_info(profile, envs_module=envs)
    assert state["module_loaded"] is True
    module._batch_invariant_MODE = True
    with pytest.raises(ValueError):
        audit.batch_invariance_info(profile, envs_module=envs)
