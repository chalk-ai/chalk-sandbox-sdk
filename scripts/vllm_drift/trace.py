"""Opt-in read-only boundary observation; never modifies weights, caches or outputs."""

from __future__ import annotations

import hashlib
import json
import re
import types

import torch

MAX_BYTES = 8 * 1024**2


def tensor_info(tensor, *, max_elements=163840):
    if not isinstance(tensor, torch.Tensor) or tensor.numel() > max_elements:
        raise ValueError("Trace tensor exceeds frozen row bound")
    value = tensor.detach().contiguous().cpu()
    data = value.view(torch.uint8).numpy().tobytes()
    if not torch.isfinite(value).all().item():
        raise ValueError("Nonfinite native trace tensor")
    return {
        "shape": list(value.shape),
        "dtype": str(value.dtype),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def cancel(worker):
    state = getattr(worker, "_ws_readonly_trace", None)
    if state is None:
        return {"cancelled": True}
    for hook in state["hooks"]:
        hook.remove()
    model = state["model"]
    if state["had_instance_compute"]:
        model.compute_logits = state["previous_instance_compute"]
    elif "compute_logits" in model.__dict__:
        delattr(model, "compute_logits")
    delattr(worker, "_ws_readonly_trace")
    return {"cancelled": True}


def arm(worker, trace_id, selected_ids, layer_indices):
    if type(trace_id) is not str or re.fullmatch(r"[0-9a-f]{32}", trace_id) is None:
        raise ValueError("Invalid trace ID")
    if (
        type(selected_ids) is not list
        or not 1 <= len(selected_ids) <= 64
        or any(type(i) is not int or not 0 <= i < 163840 for i in selected_ids)
        or len(set(selected_ids)) != len(selected_ids)
        or type(layer_indices) is not list
        or len(layer_indices) > 4
        or any(type(i) is not int for i in layer_indices)
        or len(set(layer_indices)) != len(layer_indices)
    ):
        raise ValueError("Trace selection exceeds frozen bounds")
    if getattr(worker, "_ws_readonly_trace", None) is not None:
        raise RuntimeError("A trace is already armed")
    model = worker.model_runner.get_model()
    layers = model.language_model.model.layers
    if any(not 0 <= i < len(layers) for i in layer_indices):
        raise ValueError("Invalid native layer index")
    state = {
        "model": model,
        "trace_id": trace_id,
        "records": [],
        "hooks": [],
        "had_instance_compute": "compute_logits" in model.__dict__,
        "previous_instance_compute": model.__dict__.get("compute_logits"),
        "compute_calls": 0,
        "layer_count": len(layers),
    }
    worker._ws_readonly_trace = state
    original = model.compute_logits

    def append(record):
        state["records"].append(record)
        if len(json.dumps(state["records"], allow_nan=False).encode()) > 6144:
            raise ValueError("Native per-rank trace receipt exceeds6KiB")

    def guarded(function):
        def observe(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except BaseException:
                cancel(worker)
                raise

        return observe

    @guarded
    def compute(_model, hidden_states, *args, **kwargs):
        state["compute_calls"] += 1
        if state["compute_calls"] > 1:
            raise ValueError("Read-only trace permits one single-token step")
        append({"boundary": "pre_final_norm", **tensor_info(hidden_states[-1])})
        logits = original(hidden_states, *args, **kwargs)
        if logits is not None:
            row = logits[-1]
            info = tensor_info(row)
            selected = row.detach()[selected_ids].float().cpu().tolist()
            append(
                {
                    "boundary": "raw_logits",
                    **info,
                    "selected_ids": selected_ids,
                    "selected_values": selected,
                }
            )
        else:
            append({"boundary": "raw_logits", "available": False})
        return logits

    model.compute_logits = types.MethodType(compute, model)
    try:
        for index in layer_indices:

            def hook(module, inputs, output, index=index):
                try:
                    values = output if isinstance(output, tuple) else (output,)
                    components = []
                    for component, value in enumerate(values):
                        if value is None:
                            continue
                        row = value[-1]
                        metadata = {"component_index": component}
                        if component == 1 and getattr(
                            module, "use_attn_residuals", False
                        ):
                            # Kimi allocates future residual-bank slots with
                            # new_empty; only completed/current slots are valid.
                            previous = module.prev_valid_blocks
                            writing = module.is_block_write_layer
                            if type(previous) is not int or type(writing) is not bool:
                                raise ValueError(
                                    "Invalid native residual-bank metadata"
                                )
                            valid = previous + int(writing)
                            if value.ndim != 3 or not 1 <= valid <= value.shape[1]:
                                raise ValueError("Invalid native valid residual slots")
                            row = row[:valid]
                            metadata["valid_residual_slots"] = valid
                        components.append({**metadata, **tensor_info(row)})
                    if not components:
                        raise ValueError("Missing native layer tensor components")
                    append(
                        {
                            "boundary": "layer",
                            "layer": index,
                            "components": components,
                            "sha256": hashlib.sha256(
                                json.dumps(components, sort_keys=True).encode()
                            ).hexdigest(),
                        }
                    )
                except BaseException:
                    cancel(worker)
                    raise

            state["hooks"].append(layers[index].register_forward_hook(hook))

        @guarded
        def sampler_hook(module, inputs, output):
            if output is None:
                cancel(worker)
                return
            logprobs = output.logprobs_tensors
            append(
                {
                    "boundary": "sampler",
                    "logprobs_mode": str(module.logprobs_mode),
                    "sampled_ids": output.sampled_token_ids.detach().cpu().tolist(),
                    "logprob_ids": logprobs.logprob_token_ids.detach().cpu().tolist()
                    if logprobs is not None
                    else None,
                    "logprobs": logprobs.logprobs.detach().cpu().tolist()
                    if logprobs is not None
                    else None,
                }
            )

        state["hooks"].append(
            worker.model_runner.sampler.register_forward_hook(
                sampler_hook, always_call=True
            )
        )
    except BaseException:
        cancel(worker)
        raise
    return {"armed": True, "trace_id": trace_id, "layer_count": len(layers)}


def collect(worker, trace_id):
    state = getattr(worker, "_ws_readonly_trace", None)
    if state is None or state["trace_id"] != trace_id:
        cancel(worker)
        raise ValueError("Trace ID is not the active native trace")
    try:
        if state["compute_calls"] != 1:
            raise ValueError("Missing exactly one traced native logits step")
        result = {
            "trace_id": trace_id,
            "layer_count": state["layer_count"],
            "records": state["records"],
        }
        if len(json.dumps(result, allow_nan=False).encode()) > 7168:
            raise ValueError("Native per-rank trace receipt exceeds7KiB")
        return result
    finally:
        cancel(worker)
