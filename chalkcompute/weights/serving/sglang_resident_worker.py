"""SGLang scheduler-main-loop hook for the bounded resident sparse profile."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

METHOD = "chalk_resident_sparse_control"
MAX_CONTROL_BYTES = 4 * 1024 * 1024
MAX_ACK_BYTES = 64 * 1024


def validate_control(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Exact sparse control fields required")
    fields = {
        "operation",
        "directory",
        "version",
        "manifest_sha256",
    }
    if value.get("operation") == "bind":
        fields |= {"layout_json", "layout_file_sha256"}
        if "initial_model_directory" in value:
            fields.add("initial_model_directory")
    if set(value) != fields:
        raise ValueError("Exact sparse control fields required")
    if value["operation"] not in ("bind", "apply"):
        raise ValueError("Unsupported sparse control operation")
    directory = value["directory"]
    if (
        not isinstance(directory, str)
        or len(directory) > 4096
        or not Path(directory).is_absolute()
    ):
        raise ValueError("Absolute bounded version directory required")
    if type(value["version"]) is not int or value["version"] < 0:
        raise ValueError("Nonnegative version required")
    digest = value["manifest_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("Exact manifest SHA256 required")
    if value["operation"] == "bind":
        from chalkcompute.weights.serving.adapters.resident_control import parse_layout

        if not isinstance(value["layout_json"], str):
            raise ValueError("Exact serialized layout required")
        parse_layout(value["layout_json"].encode("utf-8"), value["layout_file_sha256"])
        source = value.get("initial_model_directory", directory)
        if (
            not isinstance(source, str)
            or len(source) > 4096
            or not Path(source).is_absolute()
        ):
            raise ValueError("Absolute bounded initial model directory required")
    return value


def check_server_args(args: Any) -> None:
    expected = {
        "tp_size": 1,
        "pp_size": 1,
        "dp_size": 1,
        "ep_size": 1,
        "dtype": "bfloat16",
        "load_format": "safetensors",
        "disable_cuda_graph": True,
        "disable_radix_cache": True,
        "disable_overlap_schedule": True,
        "enable_deterministic_inference": True,
        "attention_backend": "triton",
        "weight_cache_mode": "off",
        "trust_remote_code": False,
        "quantization": None,
        "speculative_algorithm": None,
    }
    if any(
        type(getattr(args, key, object())) is not type(value)
        or getattr(args, key) != value
        for key, value in expected.items()
    ):
        raise ValueError("Resolved SGLang resident sparse profile differs")
    if args.host != "127.0.0.1" or args.tokenizer_worker_num != 1:
        raise ValueError("Single loopback HTTP tokenizer required")


def check_scheduler(scheduler: Any) -> None:
    """Admission and quiescence are checked on every native operation."""
    check_server_args(scheduler.server_args)
    if (
        scheduler.draft_worker is not None
        or scheduler.enable_overlap
        or not scheduler.is_fully_idle()
    ):
        raise ValueError("Idle single nonoverlap scheduler required")


def control(scheduler: Any, parameters: Any) -> dict[str, Any]:
    from chalkcompute.weights.serving.resident import (
        PROFILE,
        ResidentWeights,
        load_manifest,
        read_profile_file,
        validate_config,
    )
    from chalkcompute.weights.serving.resident_runtime import (
        check_cuda,
        check_runtime_sources,
    )

    request = validate_control(parameters)
    check_scheduler(scheduler)
    directory = Path(request["directory"])
    resident = getattr(scheduler, "_chalk_resident", None)
    runner = scheduler.tp_worker.model_runner
    model = runner.get_model() if hasattr(runner, "get_model") else runner.model
    if request["operation"] == "bind":
        if resident is not None:
            raise ValueError("Sparse resident already bound; restart required")
        check_runtime_sources("sglang")
        if (
            Path(scheduler.server_args.model_path).resolve()
            != Path(request.get("initial_model_directory", str(directory))).resolve()
        ):
            raise ValueError("Initial native model directory differs")
        if validate_config(
            read_profile_file(Path(scheduler.server_args.model_path), "config.json")
        ) != validate_config(read_profile_file(directory, "config.json")):
            raise ValueError("Initial native model config differs from stream seed")
        check_cuda(next(model.parameters()).device)
        manifest = load_manifest(
            directory, request["version"], request["manifest_sha256"]
        )
        from chalkcompute.weights.serving.adapters.resident_control import parse_layout

        layout = parse_layout(
            request["layout_json"].encode("utf-8"), request["layout_file_sha256"]
        )
        resident = ResidentWeights(
            model, read_profile_file(directory, "config.json"), manifest, layout
        )
        # Full initial bit equality is startup verification, excluded from apply timing.
        result = resident.verify_initial(directory)
        scheduler._chalk_resident = resident
        return {
            **result,
            "ok": True,
            "version": resident.version,
            "manifest_sha256": request["manifest_sha256"],
            "identity_sha256": resident.identity_sha256,
            "profile": PROFILE,
            "layout_sha256": resident.layout_sha256,
            "schema_sha256": resident.schema_sha256,
        }
    if resident is None:
        raise ValueError("Sparse resident must be bound before apply")
    if model is not resident.model:
        raise RuntimeError("Native resident model changed; restart required")
    result = resident.apply(directory, request["version"], request["manifest_sha256"])
    # The same native event-loop call flushes after writes and before its ACK.
    if scheduler.flush_cache() is not True:
        raise RuntimeError("Native sparse apply cache flush was refused")
    return result


def install_scheduler_hook(scheduler_class: Any, output_class: Any) -> None:
    if getattr(scheduler_class, "_chalk_resident_hook_installed", False):
        raise RuntimeError("Duplicate sparse scheduler hook")
    original = scheduler_class.handle_rpc_request

    def handle(self: Any, request: Any) -> Any:
        if request.method != METHOD:
            return original(self, request)
        try:
            result = control(self, request.parameters)
            message = json.dumps(result, allow_nan=False, separators=(",", ":"))
            if len(message.encode()) > MAX_ACK_BYTES:
                raise ValueError("Sparse ACK exceeds bound")
            return output_class(success=True, message=message)
        except Exception as error:
            # No paths, payload contents or native exception messages cross HTTP.
            return output_class(
                success=False,
                message=json.dumps({"ok": False, "error_type": type(error).__name__}),
            )

    scheduler_class.handle_rpc_request = handle
    scheduler_class._chalk_resident_hook_installed = True


def run_scheduler_process(*args: Any, **kwargs: Any) -> Any:
    io = importlib.import_module("sglang.srt.managers.io_struct")
    scheduler = importlib.import_module("sglang.srt.managers.scheduler")

    # Dispatcher construction captures this wrapper before the native loop starts.
    install_scheduler_hook(scheduler.Scheduler, io.RpcReqOutput)
    return scheduler.run_scheduler_process(*args, **kwargs)
