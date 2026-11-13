"""Worker extension for the explicitly selected single-CUDA resident sparse profile."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def check_worker(worker: Any) -> Any:
    import torch

    from chalkcompute.weights.serving.adapters.vllm_resident import validate_environment
    from chalkcompute.weights.serving.resident_runtime import check_cuda

    validate_environment(worker=True)
    config = worker.vllm_config
    parallel = config.parallel_config
    model_config = config.model_config
    if (
        parallel.tensor_parallel_size != 1
        or parallel.pipeline_parallel_size != 1
        or parallel.data_parallel_size != 1
        or config.speculative_config is not None
        or model_config.dtype != torch.bfloat16
        or model_config.quantization is not None
        or model_config.trust_remote_code
        or not model_config.enforce_eager
        or config.cache_config.enable_prefix_caching
    ):
        raise ValueError("Resolved vLLM resident sparse profile differs")
    model = worker.model_runner.get_model()
    check_cuda(next(model.parameters()).device)
    return model


class ResidentSparseExtension:
    vllm_config: Any
    model_runner: Any

    def chalk_resident_bind(
        self,
        directory: str,
        version: int,
        manifest_sha256: str,
        layout_json: str,
        layout_file_sha256: str,
        initial_model_directory: str | None = None,
    ) -> dict[str, Any]:
        from chalkcompute.weights.serving.resident import (
            PROFILE,
            ResidentWeights,
            load_manifest,
            read_profile_file,
        )
        from chalkcompute.weights.serving.resident_runtime import check_runtime_sources

        if getattr(self, "_chalk_resident", None) is not None:
            raise ValueError("Sparse resident already bound; restart required")
        check_runtime_sources("vllm")
        model = check_worker(self)
        root = Path(directory)
        if (
            Path(initial_model_directory or directory).resolve()
            != Path(self.vllm_config.model_config.model).resolve()
        ):
            raise ValueError("Initial native model directory differs")
        manifest = load_manifest(root, version, manifest_sha256)
        from chalkcompute.weights.serving.adapters.resident_control import parse_layout

        if not isinstance(layout_json, str):
            raise ValueError("Exact serialized layout required")
        layout = parse_layout(layout_json.encode("utf-8"), layout_file_sha256)
        resident = ResidentWeights(
            model, read_profile_file(root, "config.json"), manifest, layout
        )
        result = resident.verify_initial(root)
        self._chalk_resident = resident
        return {
            **result,
            "ok": True,
            "version": resident.version,
            "manifest_sha256": manifest_sha256,
            "identity_sha256": resident.identity_sha256,
            "profile": PROFILE,
            "layout_sha256": resident.layout_sha256,
            "schema_sha256": resident.schema_sha256,
        }

    def chalk_resident_apply(
        self, directory: str, version: int, manifest_sha256: str
    ) -> dict[str, Any]:
        resident = getattr(self, "_chalk_resident", None)
        if resident is None:
            raise ValueError("Sparse resident must be bound before apply")
        if check_worker(self) is not resident.model:
            raise RuntimeError("Native model object changed; restart required")
        return resident.apply(Path(directory), version, manifest_sha256)
