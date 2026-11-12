"""Actual CPU tensor/codec tests for the isolated native experiment harness."""

import base64
import importlib.util
from pathlib import Path

import pytest
import torch

ROOT = Path(__file__).parents[1] / "scripts/vllm_sparse"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


proof = load("proof")
audit = load("audit_extension")
pod = load("pod_script")


def test_all_eight_tp_shards_receive_exact_distinct_head_coordinates():
    global_indices = proof.target_indices()[proof.HEAD]
    recovered = []
    for rank in range(8):
        local = proof.local_indices(proof.HEAD, global_indices, rank)
        assert len(local) == 4
        recovered.extend(i + rank * (proof.VOCAB // 8) * proof.WIDTH for i in local)
    assert sorted(recovered) == global_indices
    assert len(recovered) == len(set(recovered)) == 32
    for rank in range(8):
        assert proof.local_indices(proof.NORM, [0, 7167], rank) == [0, 7167]


def test_actual_sparse_codec_roundtrip_and_byte_oracle(tmp_path):
    from chalkcompute._weights_codec import TensorPatch, apply_patch, read_patch

    before = torch.arange(7168, dtype=torch.float32).to(torch.bfloat16)
    indices = torch.tensor([0, 1023, 3584, 7167])
    values = torch.tensor([1.25, 2.5, 3.75, 4.0], dtype=torch.bfloat16)
    patch = TensorPatch(proof.NORM, (7168,), "bfloat16", values, indices)
    manifest = proof.write_sparse_version(
        tmp_path / "v1",
        1,
        0,
        [patch],
        proof.selected_architecture_hash(b'{"model_type":"kimi_k3"}'),
    )
    decoded = read_patch(tmp_path / "v1", manifest.tensors[0])
    after = before.clone()
    apply_patch(after, decoded)

    def raw(t):
        return t.view(torch.uint8).numpy().tobytes()

    proof.assert_exact_sparse_bytes(
        base64.b64encode(raw(before)),
        base64.b64encode(raw(after)),
        indices.tolist(),
        raw(values),
    )
    after[500] = -4
    with pytest.raises(ValueError, match="unchanged"):
        proof.assert_exact_sparse_bytes(
            base64.b64encode(raw(before)),
            base64.b64encode(raw(after)),
            indices.tolist(),
            raw(values),
        )


def test_cpu_a_b_c_inverse_uses_actual_sparse_streams_without_head_expansion(tmp_path):
    from chalkcompute._weights_codec import apply_patch, read_patch

    original = {
        proof.NORM: {i: b"\x80\x3f" for i in proof.target_indices()[proof.NORM]},
        proof.HEAD: {i: b"\x00\x3e" for i in proof.target_indices()[proof.HEAD]},
    }
    norm = torch.ones(7168, dtype=torch.bfloat16)
    baseline = audit.tensor_record(norm)["base64"]
    architecture = proof.selected_architecture_hash(b'{"model_type":"kimi_k3"}')
    for version, xor in ((1, 1), (2, 3), (3, 0)):
        root = tmp_path / str(version)
        desired = pod.build(root, original, version, version - 1, xor, architecture)
        from chalkcompute._weights_format import WeightManifest

        manifest = WeightManifest.from_json((root / "manifest.json").read_bytes())
        assert manifest.architecture_hash == architecture
        assert manifest.kind == "patch" and all(
            e.encoding == "sparse" for e in manifest.tensors
        )
        assert sum(p.stat().st_size for p in root.iterdir()) < 4096
        apply_patch(norm, read_patch(root, manifest.tensors[0]))
        proof.assert_exact_sparse_bytes(
            baseline,
            audit.tensor_record(norm)["base64"],
            proof.target_indices()[proof.NORM],
            b"".join(desired[proof.NORM].values()),
        )
        before_replay = norm.clone()
        apply_patch(norm, read_patch(root, manifest.tensors[0]))
        assert torch.equal(norm, before_replay)
    assert audit.tensor_record(norm)["base64"] == baseline


def test_dispatch_observes_actual_resident_indexed_write():
    target = torch.zeros(7168, dtype=torch.bfloat16)
    indices = torch.tensor([0, 7167])
    values = torch.tensor([1, 2], dtype=torch.bfloat16)
    guard = audit.SparseOnlyDispatch([target])
    with guard:
        target.index_copy_(0, indices, values)
    assert len(guard.sparse_writes) == 1
    assert guard.sparse_writes[0]["storage_ptr"] == target.untyped_storage().data_ptr()
    assert target[0] == 1 and target[7167] == 2 and target[1] == 0


def test_dispatch_denies_where_and_full_temporary():
    target = torch.zeros(7168, dtype=torch.bfloat16)
    condition = target == 0
    with audit.SparseOnlyDispatch([target]), pytest.raises(RuntimeError, match="where"):
        torch.where(condition, target, target)
    with audit.SparseOnlyDispatch([target]), pytest.raises(RuntimeError, match="4096B"):
        target.clone()


def test_streamed_masked_hash_proves_unchanged_bytes_without_full_clone():
    tensor = torch.arange(100000, dtype=torch.float32)
    before = audit.streamed_record(tensor, [0, 99999])
    tensor[0] = -1
    tensor[99999] = -2
    after = audit.streamed_record(tensor, [0, 99999])
    assert before["masked_sha256"] == after["masked_sha256"]
    assert before["selected_base64"] != after["selected_base64"]
    tensor[500] = -3
    assert (
        audit.streamed_record(tensor, [0, 99999])["masked_sha256"]
        != before["masked_sha256"]
    )


@pytest.mark.parametrize(
    "indices", [[-1], [proof.VOCAB * proof.WIDTH], [1, 1], [False]]
)
def test_bad_global_indices_rejected(indices):
    with pytest.raises(ValueError):
        proof.local_indices(proof.HEAD, indices, 0)


def test_repeatability_criterion_is_fixed_and_rejects_missing_nonfinite():
    import copy

    before = [
        {
            "text": "a",
            "logprobs": {
                "tokens": ["a"],
                "token_logprobs": [-1.0],
                "top_logprobs": [{"a": -1.0}],
            },
        }
    ]
    assert pod.compare_generation(before, before) == 0
    changed = copy.deepcopy(before)
    changed[0]["logprobs"]["token_logprobs"][0] += 0.002
    with pytest.raises(ValueError, match="1e-3"):
        pod.compare_generation(before, changed)
    changed[0]["logprobs"]["token_logprobs"][0] = float("nan")
    with pytest.raises(ValueError, match="Nonfinite"):
        pod.compare_generation(before, changed)
    with pytest.raises(ValueError):
        pod.compare_generation([], [])


@pytest.mark.parametrize("refusal", [True, False])
def test_actual_http500_body_is_preserved_and_generic_error_not_refusal(
    tmp_path, refusal
):
    import http.server
    import json
    import threading

    events = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            events.append(request["method"])
            failed = request["method"] == "ws_audit_corrupt_refusal"
            self.send_response(500 if failed else 200)
            self.end_headers()
            error = (
                "Checksum mismatch for " + proof.NORM
                if refusal
                else "Internal server error"
            )
            self.wfile.write(
                json.dumps(
                    {"error": error} if failed else {"results": [{}] * 8}
                ).encode()
            )

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    experiment = pod.Experiment(tmp_path, f"http://127.0.0.1:{server.server_port}")
    try:
        with pytest.raises(pod.NativeHTTPError):
            experiment.rpc("ws_audit_corrupt_refusal", [str(tmp_path), 4, "1" * 32])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert events == ["ws_audit_corrupt_refusal"]
    saved = json.loads((tmp_path / "rpc-001-error.json").read_text())
    assert saved["error"] == "HTTPError" and saved["body"]
    assert (
        "Checksum mismatch" in saved["body"]
        if refusal
        else "Internal server error" in saved["body"]
    )
