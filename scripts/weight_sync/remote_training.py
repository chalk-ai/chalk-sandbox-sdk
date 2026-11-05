"""Actual full-parameter BF16 SGD smoke, invoked by cc.training.

No allocation or heavy imports occur on import. This is SFT integration evidence,
not a GRPO/reward experiment. Instrumentation deliberately audits publisher state.
"""

from __future__ import annotations


def train_full_parameters(df, config):
    import hashlib
    import json
    import os
    import time
    from pathlib import Path

    cpu_threads = int(config["cpu_threads"])
    assert 1 <= cpu_threads <= 16
    os.environ["OMP_NUM_THREADS"] = str(cpu_threads)
    os.environ["MKL_NUM_THREADS"] = str(cpu_threads)
    import torch

    torch.set_num_threads(cpu_threads)
    from safetensors.torch import load_file
    from transformers import AutoModelForCausalLM, AutoTokenizer

    from chalkcompute._weights_format import WeightManifest, version_dir
    from chalkcompute.weights import WeightStream
    from chalkcompute.weights.serving.syncer import reload_mount

    root = Path("/chalk/weights")
    reload_mount(root)
    manifest = WeightManifest.from_json(
        (root / version_dir(0) / "manifest.json").read_bytes()
    )
    canonical = {entry.name for entry in manifest.tensors}
    stream = WeightStream(config["stream"], config["model"])
    report = {
        "status": "running",
        "kind": "full_parameter_bf16_sgd_sft",
        "training_run_id": os.environ["CHALK_TRAINING_RUN_ID"],
        "model": config["model"],
        "seed_revision": config["revision"],
        "cpu_threads": cpu_threads,
        "phases": [],
        "steps": [],
    }

    def save(phase, **extra):
        report["phases"].append({"phase": phase, "time": time.time(), **extra})
        stream.volume.put_file(
            "evidence/trainer.json", json.dumps(report, indent=2).encode()
        )
        print(json.dumps(report["phases"][-1]), flush=True)

    def state():
        result = {
            name: tensor
            for name, tensor in model.state_dict().items()
            if name in canonical
        }
        assert result.keys() == canonical
        return result

    torch.manual_seed(0)
    torch.cuda.manual_seed_all(0)
    checkpoint = root / version_dir(0)
    tokenizer = AutoTokenizer.from_pretrained(checkpoint, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        checkpoint,
        local_files_only=True,
        torch_dtype=torch.bfloat16,
        attn_implementation="sdpa",
    ).to("cuda")
    model.config.use_cache = False
    model.gradient_checkpointing_enable(
        gradient_checkpointing_kwargs={"use_reentrant": False}
    )
    assert all(parameter.requires_grad for parameter in model.parameters())
    publisher = stream.publisher(keyframe_every=50)
    save("model_loaded", parameters=sum(p.numel() for p in model.parameters()))
    publisher.resume(state())
    save("publisher_resumed")
    if hasattr(df, "collect"):
        df = df.collect()
    rows = df.to_dicts() if hasattr(df, "to_dicts") else df.to_dict("records")
    assert rows and 1 <= int(config["steps"]) <= 3

    def batch(row):
        prefix = f"Question: {row['question']}\nAnswer:"
        text = prefix + " " + row["answer"] + tokenizer.eos_token
        item = tokenizer(text, return_tensors="pt", truncation=True, max_length=128)
        labels = item["input_ids"].clone()
        prefix_length = len(tokenizer(prefix, add_special_tokens=True)["input_ids"])
        labels[:, :prefix_length] = -100
        assert (labels != -100).any(), "All answer tokens were truncated"
        return {
            **{key: value.to("cuda") for key, value in item.items()},
            "labels": labels.to("cuda"),
        }

    probe = batch(rows[0])

    def probe_loss():
        model.eval()
        with torch.no_grad():
            value = float(model(**probe).loss)
        model.train()
        return value

    report["probe_loss_before"] = probe_loss()
    optimizer = torch.optim.SGD(model.parameters(), lr=float(config["learning_rate"]))
    total_elements = sum(tensor.numel() for tensor in state().values())
    for step in range(1, int(config["steps"]) + 1):
        started = time.monotonic()
        optimizer.zero_grad(set_to_none=True)
        loss = model(**batch(rows[(step - 1) % len(rows)])).loss
        assert torch.isfinite(loss)
        loss_value = float(loss.detach())
        loss.backward()
        trainable = list(model.parameters())
        assert all(parameter.grad is not None for parameter in trainable)
        gradient_elements = sum(parameter.numel() for parameter in trainable)
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        del loss
        torch.cuda.synchronize()
        # Bitwise BF16 comparison counts actual changes before dense fallback.
        # This test-only inspection adds no third retained full-state clone.
        previous = publisher._previous
        assert previous is not None
        changed = {
            name: int(
                torch.count_nonzero(
                    value.detach().contiguous().view(torch.int16)
                    != previous[name].contiguous().view(torch.int16)
                )
            )
            for name, value in state().items()
        }
        actual_changed = sum(changed.values())
        assert actual_changed > 0
        publish_start = time.monotonic()
        publish_started_at = time.time()
        version = publisher.publish(state(), step=step)
        publish_completed_at = time.time()
        publication = WeightManifest.from_json(
            stream.volume.read_file(f"{version_dir(version)}/manifest.json")
        )
        report["steps"].append(
            {
                "step": step,
                "loss": loss_value,
                "version": version,
                "actual_changed_elements": actual_changed,
                "total_model_elements": total_elements,
                "actual_changed_fraction": actual_changed / total_elements,
                "changed_tensors": {
                    name: count for name, count in changed.items() if count
                },
                "transmitted_elements": sum(entry.nnz for entry in publication.tensors),
                "payload_bytes": sum(entry.length for entry in publication.tensors),
                "gradient_elements": gradient_elements,
                "publish_ms": (time.monotonic() - publish_start) * 1000,
                "publish_started_at": publish_started_at,
                "publish_completed_at": publish_completed_at,
                "step_and_publish_ms": (time.monotonic() - started) * 1000,
                "gpu_peak_allocated": torch.cuda.max_memory_allocated(),
            }
        )
        save("step_published", step=step, version=version)
    report["probe_loss_after"] = probe_loss()
    save("full_state_audit_begin")
    # Independently materialize the full publication chain and compare every
    # canonical tensor byte with the actual trained GPU state.
    materialized = stream.materialize(version, "/tmp/trained-checkpoint-audit")
    expected = load_file(str(materialized / "model.safetensors"))
    assert expected.keys() == canonical
    for name, value in state().items():
        current_bytes = value.detach().cpu().contiguous().reshape(-1).view(torch.uint8)
        expected_bytes = expected[name].contiguous().reshape(-1).view(torch.uint8)
        assert torch.equal(current_bytes, expected_bytes), name
    digest = hashlib.sha256()
    for name in sorted(expected):
        digest.update(name.encode())
        digest.update(
            expected[name].contiguous().reshape(-1).view(torch.uint8).numpy().tobytes()
        )
    report["full_state_audit"] = {
        "version": version,
        "tensor_count": len(expected),
        "elements": total_elements,
        "canonical_state_sha256": digest.hexdigest(),
        "all_tensor_bytes_equal": True,
    }
    report["status"] = "passed"
    save("complete")
