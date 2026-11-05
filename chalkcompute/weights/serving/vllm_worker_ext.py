"""File-only worker RPC: no torch tensor is serialized through engine IPC."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any


class WeightSyncExtension:
    # Injected by vLLM when this extension is composed into its worker.
    model_runner: Any
    device: Any

    def ws_apply_dir(
        self, directory: str, expected_version: int | str, profile: bool = False
    ) -> dict[str, object]:
        torch = importlib.import_module("torch")
        expected_version = int(expected_version)

        from chalkcompute._weights_codec import (
            decode_patch,
            payload_bytes,
            verify_payloads,
        )
        from chalkcompute._weights_format import WeightManifest
        from chalkcompute.weights.serving.profiling import WorkerTimer

        root = Path(directory)
        device = self.device
        timer = WorkerTimer(profile, torch, device)
        with timer.phase("manifest"):
            manifest = WeightManifest.from_json((root / "manifest.json").read_bytes())
        if manifest.version != expected_version:
            raise ValueError("Worker received an unexpected weight version")
        # Every rank independently verifies before touching device memory.
        with timer.phase("verify"):
            verify_payloads(root, manifest)
        model = self.model_runner.get_model()
        with torch.no_grad():
            if manifest.kind == "keyframe":
                safe_open = importlib.import_module("safetensors").safe_open
                if profile:
                    timer.counts["payload_bytes"] = sum(
                        length
                        for _, offset, length in {
                            (entry.file, entry.offset, entry.length)
                            for entry in manifest.tensors
                        }
                    )
                    timer.counts["tensors"] = len(manifest.tensors)
                    timer.counts["value_bytes"] = sum(
                        entry.nnz
                        * torch.empty(
                            (), dtype=getattr(torch, entry.dtype)
                        ).element_size()
                        for entry in manifest.tensors
                    )

                def weights():
                    for filename in sorted({entry.file for entry in manifest.tensors}):
                        with safe_open(
                            root / filename, framework="pt", device="cpu"
                        ) as checkpoint:
                            for name in checkpoint.keys():
                                yield name, checkpoint.get_tensor(name)

                # Lazy safetensors reads and native name mapping are included;
                # native load_weights does not expose separate H2D timings.
                with timer.phase("dense_load_host"):
                    model.load_weights(weights=weights())
            else:
                patches = importlib.import_module(
                    "vllm.model_executor.model_loader.checkpoint_weight_patch"
                )
                CheckpointWeightPatch = patches.CheckpointWeightPatch
                load_checkpoint_weight_patches = patches.load_checkpoint_weight_patches

                # Group sparse updates so vLLM traverses its parameter mapping
                # once per bounded batch instead of once per tensor. Dense entries
                # stay streamed; this does not duplicate a full checkpoint in RAM.
                pending = []
                pending_bytes = 0

                def flush():
                    nonlocal pending_bytes
                    if pending:
                        with timer.phase("sparse_load_host"):
                            load_checkpoint_weight_patches(
                                model, pending, validate_unique_indices=False
                            )
                        if profile:
                            timer.counts["sparse_batches"] += 1
                        pending.clear()
                        pending_bytes = 0

                for entry in manifest.tensors:
                    with timer.phase("read_checksum"):
                        payload = payload_bytes(root, entry)
                    with timer.phase("decode"):
                        patch = decode_patch(entry, payload)
                    del payload
                    if profile:
                        timer.counts["payload_bytes"] += entry.length
                        timer.counts["value_bytes"] += (
                            patch.values.numel() * patch.values.element_size()
                        )
                        timer.counts["tensors"] += 1
                    if patch.indices is None:
                        flush()
                        with timer.phase("dense_load_host"):
                            model.load_weights(
                                weights=[
                                    (patch.name, patch.values.reshape(patch.shape))
                                ]
                            )
                    else:
                        size = (
                            patch.values.numel() * patch.values.element_size()
                            + patch.indices.numel() * patch.indices.element_size()
                        )
                        if pending_bytes + size > 256 * 1024 * 1024:
                            flush()
                        if profile:
                            timer.counts["index_bytes"] += (
                                patch.indices.numel() * patch.indices.element_size()
                            )
                        with timer.phase("h2d_host"):
                            staged = CheckpointWeightPatch(
                                patch.name,
                                patch.shape,
                                getattr(torch, patch.dtype),
                                patch.values.to(device),
                                patch.indices.to(device),
                            )
                        pending.append(staged)
                        pending_bytes += size
                flush()
            if device.type == "cuda":
                with timer.phase("synchronize_host"):
                    torch.cuda.synchronize(device)
        result: dict[str, object] = {"ok": True, "version": manifest.version}
        if profile:
            result["profile"] = timer.result(self, manifest.version)
        return result

    def ws_tensor_hashes(self, names: list[str]) -> dict[str, str]:
        """Bounded internal correctness audit; callers choose exact parameter names."""
        import hashlib

        torch = importlib.import_module("torch")
        parameters = dict(self.model_runner.get_model().named_parameters())
        hashes = {}
        for name in names:
            value = parameters[name].detach().contiguous().reshape(-1).view(torch.uint8)
            hashes[name] = hashlib.sha256(value.cpu().numpy().tobytes()).hexdigest()
        return hashes
