"""CPU regression for vLLM's strict msgpack metadata boundary and failure receipts."""

import json
import sys
from types import SimpleNamespace

import pytest
import torch

from scripts.vllm_sparse import audit_extension as audit
from scripts.vllm_sparse import pod_script as pod
from scripts.vllm_sparse import proof


def test_actual_torch_version_requires_exact_str_for_msgspec():
    msgspec = pytest.importorskip("msgspec")
    from torch.torch_version import TorchVersion

    version = torch.__version__
    assert isinstance(version, TorchVersion) and type(version) is not str
    with pytest.raises(TypeError, match="TorchVersion"):
        msgspec.msgpack.encode({"torch_version": version})
    plain = str(version)
    assert type(plain) is str
    assert msgspec.msgpack.decode(msgspec.msgpack.encode({"torch_version": plain})) == {
        "torch_version": plain
    }


def test_real_snapshot_metadata_crosses_strict_msgspec_boundary_on_cpu(monkeypatch):
    msgspec = pytest.importorskip("msgspec")
    tensor = torch.nn.Parameter(torch.ones(7168, dtype=torch.bfloat16))
    model = SimpleNamespace(named_parameters=lambda: [(proof.NORM, tensor)])
    extension = audit.SparseAuditExtension()
    extension.model_runner = SimpleNamespace(get_model=lambda: model)
    extension.device = torch.device("cuda:0")
    monkeypatch.setattr(torch.distributed, "is_initialized", lambda: True)
    monkeypatch.setattr(torch.distributed, "get_rank", lambda: 0)
    monkeypatch.setattr(torch.distributed, "get_world_size", lambda: 8)
    monkeypatch.setattr(
        torch.cuda,
        "get_device_properties",
        lambda _: SimpleNamespace(uuid="fake-cpu-fixture"),
    )
    monkeypatch.setattr(
        audit,
        "Path",
        lambda _: SimpleNamespace(
            read_text=lambda: "123 (cpu fixture) " + "0 " * 19 + "456"
        ),
    )
    record = extension.ws_audit_snapshot({proof.NORM: [0, 7167]})
    assert type(record["torch_version"]) is str
    assert record["torch_version"] == str(torch.__version__)
    assert type(record["torch_hip"]) in (str, type(None))
    assert record["tensors"][proof.NORM]["device"] == "cpu"
    assert msgspec.msgpack.decode(msgspec.msgpack.encode(record)) == record
    assert not torch.cuda.is_initialized()


def test_rpc_metadata_rejects_unknown_objects_without_pickle_fallback():
    class MustNotPickle:
        def __reduce__(self):
            pytest.fail("RPC metadata must never use pickle fallback")

    with pytest.raises(TypeError, match="MustNotPickle"):
        audit.rpc_metadata({"result": [MustNotPickle()]})
    with pytest.raises(TypeError, match="TorchVersion"):
        audit.rpc_metadata({"version": torch.__version__})


@pytest.mark.parametrize("key", ["metadata", "result", "effective_config"])
def test_nested_builtin_rpc_metadata_roundtrips_without_serializer_hooks(key):
    msgspec = pytest.importorskip("msgspec")
    payload = {
        key: {
            "name": "norm",
            "shape": [7168],
            "ready": True,
            "time": 0.1,
            "optional": None,
        }
    }
    assert (
        msgspec.msgpack.decode(msgspec.msgpack.encode(audit.rpc_metadata(payload)))
        == payload
    )


@pytest.mark.parametrize("has_running_scope", [False, True])
def test_future_failure_receipt_preserves_partial_scope_and_original_error(
    tmp_path, monkeypatch, has_running_scope
):
    monkeypatch.setenv("AMD_EXPERIMENT_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pod_script",
            "--execute-reviewed",
            "--model-config",
            "unused.json",
            "--expected-config-sha256",
            "unused",
        ],
    )
    original = TypeError("Object of type TorchVersion is not serializable")

    def fail(args, directory):
        if has_running_scope:
            pod.write(
                directory / "scope.json",
                {
                    "status": "running",
                    "generation_repeatability": "not_reached",
                    "stream_drain": "not_proven",
                    "fresh_B_native_oracle": "not_proven",
                    "partial_evidence": "retained",
                },
            )
        raise original

    monkeypatch.setattr(pod, "run_experiment", fail)
    with pytest.raises(TypeError) as raised:
        pod.main()
    assert raised.value is original
    directory = tmp_path / "sparse-proof"
    scope = json.loads((directory / "scope.json").read_text())
    failure = json.loads((directory / "failure.json").read_text())
    assert scope["status"] == "failed"
    assert scope["previous_status"] == (
        "running" if has_running_scope else "not_started"
    )
    assert scope["generation_repeatability"] == "not_reached"
    assert scope["stream_drain"] == scope["fresh_B_native_oracle"] == "not_proven"
    assert scope["failure"] == failure
    assert failure["error_type"] == "TypeError" and "TorchVersion" in failure["error"]
    if has_running_scope:
        assert scope["partial_evidence"] == "retained"
