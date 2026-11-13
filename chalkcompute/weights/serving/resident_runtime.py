"""Admission for pinned resident-update engine integrations; no eager Torch import."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
from pathlib import Path
from typing import Any

RUNTIME_VERSIONS = {"vllm": "0.33.0", "sglang": "0.5.23"}
RUNTIME_PACKAGES = {
    "vllm": {"torch": "2.14.0+cu130", "transformers": "5.20.2"},
    "sglang": {"torch": "2.14.1+cu130", "transformers": "5.19.0"},
}
COMMON_PACKAGES = {"safetensors": "0.8.0", "xxhash": "3.6.0", "zstandard": "0.25.0"}
RUNTIME_SOURCES = {
    "vllm": {
        "entrypoints/serve/dev/cache/api_router.py": "8f773fff7402bfbf5a8d9290bed47a7f44cde7d248592d41264625a3f7a7e51a",
        "entrypoints/serve/dev/rpc/api_router.py": "713774687e0132ad62421c3620d5fa799f59418bbbf3597255645f811d72e902",
        "model_executor/layers/linear.py": "d4b97d850eb8bdee5fd2459e7ac688d00623b54f5ec0ea590c08da0173f8b4d4",
        "model_executor/parameter.py": "1a4bbd7400fd1ba79e8e1e8d19666cd22881ec83c35e6e01b3fdc78faee23171",
    },
    "sglang": {
        "srt/managers/scheduler_components/ipc_channels.py": "ec73fc4546bb75991daf1abf5be1b4f0fc23770eebcdb704ea0973e51285699e",
        "srt/entrypoints/engine.py": "d441ebdd8d2c2c6f44d8a3ad1390bfd9563766dc2c70f620f763597a838093f4",
        "srt/entrypoints/http_server.py": "d5d7ae6c5e8f403f9a46617377ab96977d9d4f1f263c0b90fc9fd2769eef061e",
        "srt/managers/scheduler.py": "cb8f6de62ad1e200df34114cbcffec747e16d667eddbc719ccb9052723e8b4e5",
        "srt/managers/io_struct.py": "0518aa62b3d864be6f4d76a7fcf14720a2f5854b557df19ddd8ea717de62cb57",
        "srt/utils/network.py": "da99abe51a3a18a3ee5ccb7ef1b60cd134c7b0f0f18c94927175ec2f8f102db7",
        "srt/layers/linear.py": "2c1031cfaafe329a71ac5faf6215a8eb21206b79f8a5e7d821f21bb810c13afc",
    },
}


def check_runtime_sources(kind: str) -> None:
    if kind not in RUNTIME_VERSIONS:
        raise ValueError("Unsupported Resident sparse runtime")
    if importlib.metadata.version(kind) != RUNTIME_VERSIONS[kind]:
        raise ValueError(
            f"Resident sparse serving requires {kind} {RUNTIME_VERSIONS[kind]}"
        )
    for package, expected in {**COMMON_PACKAGES, **RUNTIME_PACKAGES[kind]}.items():
        if importlib.metadata.version(package) != expected:
            raise ValueError(f"Resident sparse serving requires {package} {expected}")
    spec = importlib.util.find_spec(kind)
    if spec is None or spec.origin is None:
        raise ValueError("Native runtime package source is unavailable")
    root = Path(spec.origin).parent
    for name, expected in RUNTIME_SOURCES[kind].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Unreviewed native runtime source: {name}")


def check_cuda(device: Any) -> None:
    """Require one NVIDIA CUDA device with native BF16 support.

    This capability check is not a claim of GPU-model qualification. Concrete
    engines, layouts and hardware still require their own correctness run.
    """
    import torch

    device = torch.device(device)
    if (
        device.type != "cuda"
        or torch.version.cuda is None
        or torch.version.hip is not None
        or torch.cuda.device_count() != 1
    ):
        raise ValueError("Resident sparse updates require one BF16-capable CUDA device")
    with torch.cuda.device(device):
        if not torch.cuda.is_bf16_supported(including_emulation=False):
            raise ValueError("Resident sparse updates require native CUDA BF16 support")
