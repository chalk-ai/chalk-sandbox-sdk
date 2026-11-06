"""File-only worker RPC: no torch tensor is serialized through engine IPC."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any


class WeightSyncExtension:
    # Injected by vLLM when this extension is composed into its worker.
    model_runner: Any
    device: Any

    def ws_sparse_probe(self) -> dict[str, object]:
        from chalkcompute.weights.serving.vllm_resident_sparse import probe

        return probe(self)

    def ws_sparse_prepare_dir(
        self, directory: str, expected_version: int, transaction_id: str
    ) -> dict[str, object]:
        from chalkcompute.weights.serving.vllm_resident_sparse import prepare

        return prepare(self, directory, expected_version, transaction_id)

    def ws_sparse_commit(
        self,
        transaction_id: str,
        expected_version: int,
        rank_identities: dict[str, str],
        manifest_sha256: str,
    ) -> dict[str, object]:
        from chalkcompute.weights.serving.vllm_resident_sparse import commit

        return commit(
            self, transaction_id, expected_version, rank_identities, manifest_sha256
        )

    def ws_sparse_abort(self, transaction_id: str) -> dict[str, object]:
        from chalkcompute.weights.serving.vllm_resident_sparse import abort

        return abort(self, transaction_id)

    def ws_apply_dir(
        self, directory: str, expected_version: int | str
    ) -> dict[str, object]:
        torch = importlib.import_module("torch")
        expected_version = int(expected_version)

        from chalkcompute._weights_codec import read_patch, verify_payloads
        from chalkcompute._weights_format import WeightManifest

        root = Path(directory)
        manifest = WeightManifest.from_json((root / "manifest.json").read_bytes())
        if manifest.version != expected_version:
            raise ValueError("Worker received an unexpected weight version")
        # Every rank independently verifies before touching device memory.
        verify_payloads(root, manifest)
        model = self.model_runner.get_model()
        device = self.device
        with torch.no_grad():
            if manifest.kind == "keyframe":
                safe_open = importlib.import_module("safetensors").safe_open

                def weights():
                    for filename in sorted({entry.file for entry in manifest.tensors}):
                        with safe_open(
                            root / filename, framework="pt", device="cpu"
                        ) as checkpoint:
                            for name in checkpoint.keys():
                                yield name, checkpoint.get_tensor(name)

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
                        load_checkpoint_weight_patches(
                            model, pending, validate_unique_indices=False
                        )
                        pending.clear()
                        pending_bytes = 0

                for entry in manifest.tensors:
                    patch = read_patch(root, entry)
                    if patch.indices is None:
                        flush()
                        model.load_weights(
                            weights=[(patch.name, patch.values.reshape(patch.shape))]
                        )
                    else:
                        size = (
                            patch.values.numel() * patch.values.element_size()
                            + patch.indices.numel() * patch.indices.element_size()
                        )
                        if pending_bytes + size > 256 * 1024 * 1024:
                            flush()
                        pending.append(
                            CheckpointWeightPatch(
                                patch.name,
                                patch.shape,
                                getattr(torch, patch.dtype),
                                patch.values.to(device),
                                patch.indices.to(device),
                            )
                        )
                        pending_bytes += size
                flush()
            if device.type == "cuda":
                torch.cuda.synchronize(device)
        return {"ok": True, "version": manifest.version}

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
