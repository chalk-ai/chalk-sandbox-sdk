"""Opt-in measured BF16/TP1 indexed updates; never full-loader fallback."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sys
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout

from chalkcompute._weights_format import WeightManifest
from chalkcompute.weights.serving.adapters.resident_control import LayoutSnapshot
from chalkcompute.weights.serving.adapters.vllm import VLLMAdapter
from chalkcompute.weights.serving.resident import (
    PROFILE,
    load_manifest,
    read_profile_file,
    validate_config,
)
from chalkcompute.weights.serving.resident_runtime import check_runtime_sources


def validate_environment(*, worker: bool = False) -> None:
    required = {
        "VLLM_SERVER_DEV_MODE": "1",
        "VLLM_WORKER_MULTIPROC_METHOD": "spawn",
    }
    allowed = {key: {value} for key, value in required.items()}
    # Verification can opt into batch-invariant kernels; sparse writes do not
    # require them. Preserve an explicit choice or leave vLLM's default alone.
    allowed["VLLM_BATCH_INVARIANT"] = {"0", "1"}
    if worker:
        if any(os.environ.get(key) != value for key, value in required.items()):
            raise ValueError("Required native resident worker environment differs")
        # Pinned native worker startup sets this internal marker. It may be
        # absent when OMP_NUM_THREADS was already supplied by the container.
        allowed["VLLM_OMP_NUM_THREADS_SET_BY_VLLM"] = {"1"}
    if any(
        key.startswith("VLLM_") and value not in allowed.get(key, set())
        for key, value in os.environ.items()
    ):
        raise ValueError("Unreviewed VLLM environment override")


class VLLMResidentAdapter(VLLMAdapter):
    def __init__(
        self,
        port: int = 8001,
        engine_args: list[str] | None = None,
        *,
        weight_layout: Path,
        weight_layout_sha256: str,
    ):
        if type(port) is not int or not 0 < port < 65536:
            raise ValueError("Valid native loopback port required")
        super().__init__(port, engine_args)
        self._layout = LayoutSnapshot(weight_layout, weight_layout_sha256)
        self._model_dir: Path | None = None
        self._identity: str | None = None
        self._failed = False
        index = 0
        seen: set[str] = set()
        while index < len(self.engine_args):
            option, separator, value = self.engine_args[index].partition("=")
            if (
                option not in ("--gpu-memory-utilization", "--max-model-len")
                or option in seen
            ):
                raise ValueError("Only unique memory/context tuning is supported")
            seen.add(option)
            if not separator:
                index += 1
                if index == len(self.engine_args):
                    raise ValueError("Missing native option value")
                value = self.engine_args[index]
            if option == "--max-model-len":
                if not 0 < int(value) <= 4096:
                    raise ValueError("Context length must be between 1 and 4096")
            elif not math.isfinite(float(value)) or not 0 < float(value) < 1:
                raise ValueError(
                    "Memory utilization must be finite and between zero and one"
                )
            index += 1

    def launch_cmd(self, model_dir: Path, port: int) -> list[str]:
        if type(port) is not int or self.url != f"http://127.0.0.1:{port}":
            raise ValueError("Native launch port differs from adapter")
        validate_config(read_profile_file(model_dir, "config.json"))
        check_runtime_sources("vllm")
        validate_environment()
        self._model_dir = model_dir.resolve()
        return [
            sys.executable,
            "-m",
            "chalkcompute.weights.serving.vllm_resident_server",
            "--model",
            str(self._model_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--worker-extension-cls",
            "chalkcompute.weights.serving.vllm_resident_worker.ResidentSparseExtension",
            "--dtype",
            "bfloat16",
            "--tensor-parallel-size",
            "1",
            "--pipeline-parallel-size",
            "1",
            "--load-format",
            "safetensors",
            "--enforce-eager",
            "--no-enable-prefix-caching",
            "--max-num-seqs",
            "1",
            "--seed",
            "42",
            *(
                []
                if any(arg.startswith("--max-model-len") for arg in self.engine_args)
                else ["--max-model-len", "256"]
            ),
            *(
                []
                if any(
                    arg.startswith("--gpu-memory-utilization")
                    for arg in self.engine_args
                )
                else ["--gpu-memory-utilization", "0.5"]
            ),
            *self.engine_args,
        ]

    async def _post(self, path: str, body: dict) -> object:
        async with ClientSession(timeout=ClientTimeout(total=600)) as client:
            async with client.post(self.url + path, json=body) as response:
                if response.status != 200:
                    raise RuntimeError("Native sparse control was not acknowledged")
                try:
                    await response.content.readexactly(64 * 1024 + 1)
                except asyncio.IncompleteReadError as error:
                    raw = error.partial
                else:
                    raise RuntimeError("Native sparse ACK exceeds bound")
                result = json.loads(raw)
                if (
                    not isinstance(result, dict)
                    or result.get("error")
                    or result.get("success") is False
                ):
                    raise RuntimeError("Native sparse control refused")
                return result

    async def _control(
        self, method: str, directory: Path, manifest: WeightManifest
    ) -> None:
        if self._failed:
            raise RuntimeError("Uncertain sparse adapter; restart required")
        digest = hashlib.sha256(
            read_profile_file(directory, "manifest.json")
        ).hexdigest()
        if load_manifest(directory, manifest.version, digest) != manifest:
            raise ValueError("Control manifest differs from observed manifest")
        try:
            response = await self._post(
                "/collective_rpc",
                {
                    "method": method,
                    "args": [str(directory.resolve()), manifest.version, digest]
                    + (
                        [
                            self._layout.raw.decode("utf-8"),
                            self._layout.file_sha256,
                            str(self._model_dir),
                        ]
                        if method == "chalk_resident_bind"
                        else []
                    ),
                },
            )
            results = response.get("results") if isinstance(response, dict) else None
            if not isinstance(results, list) or len(results) != 1:
                raise RuntimeError("Exactly one native sparse worker ACK required")
            result = results[0]
            if not isinstance(result, dict):
                raise RuntimeError("Invalid native sparse ACK")
            identity = result.get("identity_sha256")
            if (
                result.get("ok") is not True
                or type(result.get("version")) is not int
                or result["version"] != manifest.version
                or result.get("manifest_sha256") != digest
                or result.get("profile") != PROFILE
                or not self._layout.check_ack(result)
                or not isinstance(identity, str)
                or len(identity) != 64
                or any(c not in "0123456789abcdef" for c in identity)
                or (self._identity is not None and identity != self._identity)
            ):
                raise RuntimeError(
                    "Native sparse ACK identity/version/manifest differs"
                )
            self._identity = identity
        except BaseException:
            self._failed = True
            raise

    async def initialize(self, model_dir: Path, manifest: WeightManifest) -> None:
        if (
            self._model_dir is None
            or manifest.kind != "keyframe"
            or self._identity is not None
        ):
            raise ValueError("Bind requires the launched initial keyframe")
        self._layout.check_manifest(manifest)
        await self._control("chalk_resident_bind", model_dir, manifest)

    async def apply(self, version_dir: Path, manifest: WeightManifest) -> None:
        if self._identity is None:
            raise RuntimeError("Native sparse resident is not bound")
        await self._control("chalk_resident_apply", version_dir, manifest)

    async def reset_cache(self) -> None:
        if self._failed or self._identity is None:
            raise RuntimeError("Healthy bound sparse resident required")
        try:
            await super().reset_cache()
        except BaseException:
            self._failed = True
            raise
