"""Test-only worker instrumentation; no model loading or resource lifecycle.

Compose this extension only in a reviewed experiment image/overlay. The real SDK
worker method remains the implementation under test. Runtime guards deliberately
make unsupported full-loader fallbacks fail instead of counting them as sparse work.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import math
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import torch
from torch.utils._python_dispatch import TorchDispatchMode

from chalkcompute.weights.serving.vllm_worker_ext import WeightSyncExtension

MAX_INSPECTED_BYTES = 128 * 1024
MAX_PATCH_NNZ = 64
MAX_TEMPORARY_BYTES = 4096


def rpc_metadata(value):
    """Reject non-builtin metadata before vLLM's strict msgpack boundary."""
    if type(value) in (str, int, bool, float, type(None)):
        return value
    if type(value) is list:
        for item in value:
            rpc_metadata(item)
        return value
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            rpc_metadata(item)
        return value
    raise TypeError(f"Unsupported audit RPC metadata type: {type(value).__name__}")


def batch_invariance_info(profile, *, batch_module=None, envs_module=None):
    """Observe pinned private state; never enable or initialize batch mode here."""
    enabled = profile["batch_invariant"]
    if batch_module is None:
        name = "vllm.model_executor.determinism.batch_invariant"
        batch_module = (
            importlib.import_module(name) if enabled else sys.modules.get(name)
        )
    envs_module = envs_module or importlib.import_module("vllm.envs")
    module_hash = (
        hashlib.sha256(Path(batch_module.__file__).read_bytes()).hexdigest()
        if batch_module is not None
        else None
    )
    if (
        enabled
        and module_hash
        != profile["source_sha256"][
            "vllm/model_executor/determinism/batch_invariant.py"
        ]
    ):
        raise ValueError("Pinned batch-invariance source differs")
    controls = {
        "matmul_fp32_precision": torch.backends.cuda.matmul.fp32_precision,
        "cudnn_conv_fp32_precision": torch.backends.cudnn.conv.fp32_precision,
        "cudnn_rnn_fp32_precision": torch.backends.cudnn.rnn.fp32_precision,
        "allow_fp16_reduced_precision_reduction": torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction,
        "allow_bf16_reduced_precision_reduction": torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction,
    }
    controls = {
        key: list(value) if type(value) is tuple else value
        for key, value in controls.items()
    }
    state = rpc_metadata(
        {
            "expected_enabled": enabled,
            "environment_value": os.environ.get("VLLM_BATCH_INVARIANT"),
            "resolved_env_flag": envs_module.VLLM_BATCH_INVARIANT,
            "module_loaded": batch_module is not None,
            "private_mode": batch_module._batch_invariant_MODE
            if batch_module is not None
            else None,
            "private_library_present": (batch_module._batch_invariant_LIB is not None)
            if batch_module is not None
            else None,
            "module_sha256": module_hash,
            "public_backend_controls": controls,
            "scope": "read-only source-bound private state and public controls; no end-to-end numerical guarantee",
        }
    )
    if (
        any(state[key] is not enabled for key in ("resolved_env_flag",))
        or any(
            state[key] is not (enabled if batch_module is not None else None)
            for key in ("private_mode", "private_library_present")
        )
        or state["environment_value"] not in (("1",) if enabled else (None, "0"))
    ):
        raise ValueError(
            "Actual worker batch-invariance state differs from frozen expectation"
        )
    return state


def tensor_record(tensor, indices=None):
    """Read one explicitly bounded tensor; never copy a full model to CPU."""
    if tensor.numel() * tensor.element_size() > MAX_INSPECTED_BYTES:
        return streamed_record(tensor, indices)
    raw = tensor.detach().contiguous().reshape(-1).view(torch.uint8)
    data = raw.cpu().numpy().tobytes()
    return {
        "shape": list(tensor.shape),
        "dtype": str(tensor.dtype).removeprefix("torch."),
        "device": str(tensor.device),
        "storage_ptr": tensor.untyped_storage().data_ptr(),
        "data_ptr": tensor.data_ptr(),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "base64": base64.b64encode(data).decode(),
    }


def streamed_record(tensor, indices):
    """Hash all unchanged bytes in <=4MiB CPU chunks; retain tiny indexed samples."""
    if (
        not tensor.is_contiguous()
        or tensor.numel() * tensor.element_size() > 512 * 1024**2
    ):
        raise ValueError("Only contiguous inspected shards <=512MiB are supported")
    if (
        not isinstance(indices, list)
        or not 1 <= len(indices) <= MAX_PATCH_NNZ
        or indices != sorted(set(indices))
    ):
        raise ValueError("Require bounded sorted distinct local sample indices")
    if any(type(i) is not int or not 0 <= i < tensor.numel() for i in indices):
        raise ValueError("Sample indices out of bounds")
    size = tensor.element_size()
    flat = tensor.detach().reshape(-1)
    digest = hashlib.sha256()
    elements = (4 * 1024**2) // size
    samples = {}
    for start in range(0, flat.numel(), elements):
        end = min(start + elements, flat.numel())
        raw = bytearray(flat[start:end].view(torch.uint8).cpu().numpy().tobytes())
        for index in indices:
            if start <= index < end:
                offset = (index - start) * size
                samples[str(index)] = base64.b64encode(
                    raw[offset : offset + size]
                ).decode()
                raw[offset : offset + size] = b"\0" * size
        digest.update(raw)
    return {
        "shape": list(tensor.shape),
        "dtype": str(tensor.dtype).removeprefix("torch."),
        "device": str(tensor.device),
        "storage_ptr": tensor.untyped_storage().data_ptr(),
        "data_ptr": tensor.data_ptr(),
        "bytes": tensor.numel() * size,
        "masked_sha256": digest.hexdigest(),
        "omitted_indices": indices,
        "selected_base64": samples,
        "maximum_cpu_chunk_bytes": 4 * 1024**2,
    }


def _tensors(value):
    if isinstance(value, torch.Tensor):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _tensors(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _tensors(item)


class SparseOnlyDispatch(TorchDispatchMode):
    """Bound all newly allocated tensor storage during the actual worker call."""

    def __init__(self, resident_tensors):
        super().__init__()
        self.resident = {
            (str(t.device), t.untyped_storage().data_ptr()) for t in resident_tensors
        }
        self.sparse_writes = []
        self.max_new_storage_bytes = 0

    def __torch_dispatch__(self, func, types, args=(), kwargs=None):
        kwargs = kwargs or {}
        name = str(func)
        if "where" in name:
            raise RuntimeError("Full tensor torch.where fallback forbidden")
        # Refuse common full-sized factories/copies BEFORE allocation. The output
        # storage check below additionally audits operations without a shape-only
        # factory signature; this is instrumentation, not a general device quota.
        shape = None
        dtype = kwargs.get("dtype", torch.get_default_dtype())
        if any(
            name.startswith("aten." + op + ".")
            for op in ("empty", "empty_strided", "zeros", "ones", "full")
        ):
            shape = args[0]
        elif any(
            name.startswith("aten." + op + ".")
            for op in (
                "clone",
                "_to_copy",
                "empty_like",
                "zeros_like",
                "ones_like",
                "full_like",
            )
        ):
            shape, dtype = args[0].shape, kwargs.get("dtype", args[0].dtype)
        elif any(
            name.startswith("aten.new_" + op + ".")
            for op in ("empty", "zeros", "ones", "full")
        ):
            shape, dtype = args[1], kwargs.get("dtype", args[0].dtype)
        if (
            shape is not None
            and math.prod(shape) * torch.empty(0, dtype=dtype).element_size()
            > MAX_TEMPORARY_BYTES
        ):
            raise RuntimeError(
                "New tensor allocation exceeds4096B sparse budget before allocation"
            )
        outputs = func(*args, **kwargs)
        for tensor in _tensors(outputs):
            storage = tensor.untyped_storage()
            if (str(tensor.device), storage.data_ptr()) not in self.resident:
                size = storage.nbytes()
                self.max_new_storage_bytes = max(self.max_new_storage_bytes, size)
                if size > MAX_TEMPORARY_BYTES:
                    raise RuntimeError(
                        "New tensor allocation exceeds4096B sparse budget"
                    )
        if any(op in name for op in ("index_copy_", "index_put_", "scatter_")):
            destination = args[0]
            if (
                str(destination.device),
                destination.untyped_storage().data_ptr(),
            ) not in self.resident:
                raise RuntimeError("Sparse write did not target resident model storage")
            self.sparse_writes.append(
                {"op": name, "storage_ptr": destination.untyped_storage().data_ptr()}
            )
        return outputs


def forbidden_loader(*args, **kwargs):
    raise RuntimeError("Full/model/checkpoint-helper loading forbidden in sparse phase")


class SparseAuditExtension(WeightSyncExtension):
    """Additional bounded diagnostics around the actual production sparse method."""

    def ws_audit_runtime_info(self):
        from .proof import expected_runtime_profile

        config = self.vllm_config
        actual = {
            "enforce_eager": config.model_config.enforce_eager,
            "prefix_caching": config.cache_config.enable_prefix_caching,
            "mamba_cache_mode": config.cache_config.mamba_cache_mode,
            "cache_dtype": config.cache_config.cache_dtype,
            "mamba_ssm_cache_dtype": config.cache_config.mamba_ssm_cache_dtype,
            "speculative_config": config.speculative_config,
            "tp": config.parallel_config.tensor_parallel_size,
            "pp": config.parallel_config.pipeline_parallel_size,
            "dp": config.parallel_config.data_parallel_size,
            "dcp": config.parallel_config.decode_context_parallel_size,
            "pcp": config.parallel_config.prefill_context_parallel_size,
            "expert_parallel": config.parallel_config.enable_expert_parallel,
            "distributed_executor_backend": config.parallel_config.distributed_executor_backend,
            "compilation_mode": config.compilation_config.mode.name,
            "cudagraph_mode": config.compilation_config.cudagraph_mode.name,
            "offload_backend": config.offload_config.offload_backend,
            "cpu_offload_gb": config.offload_config.uva.cpu_offload_gb,
            "offload_group_size": config.offload_config.prefetch.offload_group_size,
            "kv_offloading_size": config.cache_config.kv_offloading_size,
            "kv_transfer_config": config.kv_transfer_config,
        }
        expected = {
            "enforce_eager": True,
            "prefix_caching": False,
            "mamba_cache_mode": "none",
            "cache_dtype": "fp8",
            "mamba_ssm_cache_dtype": "float32",
            "speculative_config": None,
            "tp": 8,
            "pp": 1,
            "dp": 1,
            "dcp": 1,
            "pcp": 1,
            "expert_parallel": False,
            "distributed_executor_backend": "mp",
            "compilation_mode": "NONE",
            "cudagraph_mode": "NONE",
            "offload_backend": "auto",
            "cpu_offload_gb": 0,
            "offload_group_size": 0,
            "kv_offloading_size": None,
            "kv_transfer_config": None,
        }
        if actual != expected:
            raise ValueError(f"Effective worker runtime config differs: {actual}")
        return rpc_metadata(
            {
                **super().ws_sparse_probe(),
                "effective_config": actual,
                "batch_invariance": batch_invariance_info(expected_runtime_profile()),
            }
        )

    def ws_audit_snapshot(self, targets):
        from .proof import local_indices

        if not isinstance(targets, dict) or not 1 <= len(targets) <= 3:
            raise ValueError("Require1..3 distinct exact resident parameter names")
        if self.device.type != "cuda" or not torch.distributed.is_initialized():
            raise RuntimeError("Native distributed ROCm/CUDA workers required")
        model = self.model_runner.get_model()
        parameters = dict(model.named_parameters())
        props = torch.cuda.get_device_properties(self.device)
        return rpc_metadata(
            {
                "rank": torch.distributed.get_rank(),
                "world_size": torch.distributed.get_world_size(),
                "pid": os.getpid(),
                "process_start_ticks": Path("/proc/self/stat")
                .read_text()
                .rsplit(")", 1)[1]
                .split()[19],
                "device": str(self.device),
                "device_uuid": str(getattr(props, "uuid", "unavailable")),
                "model_id": id(model),
                "torch_version": str(torch.__version__),
                "torch_hip": str(torch.version.hip)
                if torch.version.hip is not None
                else None,
                "tensors": {
                    name: tensor_record(
                        parameters[name],
                        local_indices(name, indices, torch.distributed.get_rank())
                        if indices
                        else None,
                    )
                    for name, indices in targets.items()
                },
            }
        )

    def ws_audit_prepare_dir(self, directory, expected_version, transaction_id):
        from chalkcompute._weights_codec import verify_payloads
        from chalkcompute._weights_format import WeightManifest

        root = Path(directory)
        started = time.perf_counter()
        manifest = WeightManifest.from_json((root / "manifest.json").read_bytes())
        if (
            manifest.kind != "patch"
            or not manifest.tensors
            or any(
                entry.encoding != "sparse" or not 0 < entry.nnz <= MAX_PATCH_NNZ
                for entry in manifest.tensors
            )
        ):
            raise ValueError(
                "Measured phase permits only bounded nonempty sparse entries"
            )
        verify_payloads(root, manifest)
        result = super().ws_sparse_prepare_dir(
            directory, expected_version, transaction_id
        )
        return rpc_metadata(
            {
                **result,
                "prepare_verify_and_stage_seconds": time.perf_counter() - started,
                "compressed_payload_bytes": sum(e.length for e in manifest.tensors),
                "global_sparse_values_bytes": sum(e.nnz * 2 for e in manifest.tensors),
                "global_logical_int64_index_bytes": sum(
                    e.nnz * 8 for e in manifest.tensors
                ),
            }
        )

    def ws_audit_corrupt_refusal(self, directory, expected_version, transaction_id):
        from chalkcompute._weights_format import WeightStreamError
        from .proof import NORM, corrupt_payload_evidence

        if expected_version != 4:
            raise ValueError("Corruption refusal is restricted to version4")
        before = super().ws_sparse_probe()
        if (
            before["version"] != 3
            or getattr(self, "_ws_resident_pending", None) is not None
            or getattr(self, "_ws_resident_failed", False) is not False
        ):
            raise RuntimeError("Corruption refusal requires clean committed version3")
        evidence = corrupt_payload_evidence(directory)
        model = self.model_runner.get_model()
        guard = SparseOnlyDispatch([*model.parameters(), *model.buffers()])
        try:
            with guard:
                self.ws_audit_prepare_dir(directory, expected_version, transaction_id)
        except WeightStreamError as error:
            if (
                type(error) is not WeightStreamError
                or str(error) != "Checksum mismatch for " + NORM
            ):
                raise
        else:
            raise RuntimeError("Corrupted payload unexpectedly prepared")
        after = super().ws_sparse_probe()
        if (
            before != after
            or guard.sparse_writes
            or getattr(self, "_ws_resident_pending", None) is not None
            or getattr(self, "_ws_resident_failed", False) is not False
            or evidence != corrupt_payload_evidence(directory)
        ):
            raise RuntimeError("Corrupt preparation changed resident state or payload")
        return rpc_metadata(
            {
                **before,
                **evidence,
                "requested_version": expected_version,
                "transaction_id": transaction_id,
                "refused": True,
                "error_type": "WeightStreamError",
                "error": "Checksum mismatch for " + NORM,
                "pending_before": False,
                "pending_after": False,
                "failed_before": False,
                "failed_after": False,
                "version_after": after["version"],
                "sparse_writes": guard.sparse_writes,
            }
        )

    def ws_audit_commit(
        self, transaction_id, expected_version, rank_identities, manifest_sha256
    ):
        model = self.model_runner.get_model()
        previous = getattr(self, "_ws_resident_last_commit", None)
        replay = isinstance(previous, dict) and all(
            previous.get(key) == value
            for key, value in {
                "transaction_id": transaction_id,
                "version": expected_version,
                "manifest_sha256": manifest_sha256,
            }.items()
        )
        guard = SparseOnlyDispatch([*model.parameters(), *model.buffers()])
        with ExitStack() as stack:
            stack.enter_context(patch.object(model, "load_weights", forbidden_loader))
            stack.enter_context(patch.object(torch, "where", forbidden_loader))
            from vllm.model_executor.model_loader import checkpoint_weight_patch

            stack.enter_context(
                patch.object(
                    checkpoint_weight_patch,
                    "load_checkpoint_weight_patches",
                    forbidden_loader,
                )
            )
            stack.enter_context(guard)
            started = time.perf_counter()
            result = super().ws_sparse_commit(
                transaction_id, expected_version, rank_identities, manifest_sha256
            )
            commit_seconds = time.perf_counter() - started
        if not guard.sparse_writes and not replay:
            raise RuntimeError(
                "No actual indexed write to resident model storage observed"
            )
        if replay and guard.sparse_writes:
            raise RuntimeError("Exact transaction replay performed a native mutation")
        return rpc_metadata(
            {
                "result": result,
                "idempotent_no_write_replay": replay,
                "commit_and_sync_seconds": commit_seconds,
                "rank": torch.distributed.get_rank(),
                "pid": os.getpid(),
                "sparse_writes": guard.sparse_writes,
                "maximum_new_storage_bytes": guard.max_new_storage_bytes,
                "full_loaders_forbidden": True,
            }
        )


# Opt-in read-only localization is isolated from sparse prepare/commit methods.
def _localization_rpc(extension, operation, *args):
    from scripts.vllm_drift import trace

    result = getattr(trace, operation)(extension, *args)
    return rpc_metadata(
        {"rank": torch.distributed.get_rank(), "pid": os.getpid(), **result}
    )


def _localization_arm(self, trace_id, selected_ids, layer_indices):
    return _localization_rpc(self, "arm", trace_id, selected_ids, layer_indices)


def _localization_collect(self, trace_id):
    return _localization_rpc(self, "collect", trace_id)


def _localization_cancel(self):
    return _localization_rpc(self, "cancel")


SparseAuditExtension.ws_localization_arm = _localization_arm
SparseAuditExtension.ws_localization_collect = _localization_collect
SparseAuditExtension.ws_localization_cancel = _localization_cancel
