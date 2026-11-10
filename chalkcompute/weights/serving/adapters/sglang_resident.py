"""Opt-in BF16/TP1 sparse SGLang adapter; no dense fallback."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sys
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout

from chalkcompute._weights_format import WeightManifest
from chalkcompute.weights.serving.adapters.resident_control import LayoutSnapshot
from chalkcompute.weights.serving.adapters.sglang import _UPSTREAM_ENV
from chalkcompute.weights.serving.resident import (
    PROFILE,
    load_manifest,
    read_profile_file,
    validate_config,
)
from chalkcompute.weights.serving.resident_runtime import check_runtime_sources
from chalkcompute.weights.serving.sglang_resident_server import CONTROL_PATH


def validate_environment() -> None:
    if any(
        key.startswith("SGLANG_") and _UPSTREAM_ENV.get(key) != value
        for key, value in os.environ.items()
    ):
        raise ValueError("Unreviewed SGLANG environment override")


class SGLangResidentAdapter:
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
        self.url = f"http://127.0.0.1:{port}"
        self.engine_args = list(engine_args or [])
        self._layout = LayoutSnapshot(weight_layout, weight_layout_sha256)
        self._model_dir: Path | None = None
        self._identity: str | None = None
        self._failed = False
        seen: set[str] = set()
        index = 0
        while index < len(self.engine_args):
            option, separator, value = self.engine_args[index].partition("=")
            if (
                option not in ("--mem-fraction-static", "--context-length")
                or option in seen
            ):
                raise ValueError("Only unique memory/context tuning is supported")
            seen.add(option)
            if not separator:
                index += 1
                if index == len(self.engine_args):
                    raise ValueError("Missing native option value")
                value = self.engine_args[index]
            if option == "--context-length":
                if not 0 < int(value) <= 4096:
                    raise ValueError("Context length must be between 1 and 4096")
            elif not math.isfinite(float(value)) or not 0 < float(value) < 1:
                raise ValueError(
                    "Memory fraction must be finite and between zero and one"
                )
            index += 1

    def launch_cmd(self, model_dir: Path, port: int) -> list[str]:
        if type(port) is not int or self.url != f"http://127.0.0.1:{port}":
            raise ValueError("Native launch port differs from adapter")
        validate_config(read_profile_file(model_dir, "config.json"))
        check_runtime_sources("sglang")
        validate_environment()
        self._model_dir = model_dir.resolve()
        return [
            sys.executable,
            "-m",
            "chalkcompute.weights.serving.sglang_resident_server",
            "--model-path",
            str(self._model_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--dtype",
            "bfloat16",
            "--tp-size",
            "1",
            "--pp-size",
            "1",
            "--dp-size",
            "1",
            "--ep-size",
            "1",
            "--load-format",
            "safetensors",
            "--weight-cache-mode",
            "off",
            "--disable-cuda-graph",
            "--disable-radix-cache",
            "--disable-overlap-schedule",
            "--enable-deterministic-inference",
            "--attention-backend",
            "triton",
            "--max-running-requests",
            "1",
            "--random-seed",
            "42",
            *(
                []
                if any(arg.startswith("--context-length") for arg in self.engine_args)
                else ["--context-length", "256"]
            ),
            *(
                []
                if any(
                    arg.startswith("--mem-fraction-static") for arg in self.engine_args
                )
                else ["--mem-fraction-static", "0.5"]
            ),
            *self.engine_args,
        ]

    async def _post(self, path: str, body: dict, *, plain: bool = False) -> dict:
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
                if plain:
                    return {}
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise RuntimeError("Native sparse ACK must be an object")
                return result

    async def _control(
        self, operation: str, directory: Path, manifest: WeightManifest
    ) -> None:
        if self._failed:
            raise RuntimeError("Uncertain sparse adapter; restart required")
        raw = read_profile_file(directory, "manifest.json")
        digest = hashlib.sha256(raw).hexdigest()
        if load_manifest(directory, manifest.version, digest) != manifest:
            raise ValueError("Control manifest differs from observed manifest")
        try:
            result = await self._post(
                CONTROL_PATH,
                {
                    "operation": operation,
                    "directory": str(directory.resolve()),
                    "version": manifest.version,
                    "manifest_sha256": digest,
                    **(
                        {
                            "layout_json": self._layout.raw.decode("utf-8"),
                            "layout_file_sha256": self._layout.file_sha256,
                        }
                        if operation == "bind"
                        else {}
                    ),
                },
            )
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
            self._model_dir != model_dir.resolve()
            or manifest.kind != "keyframe"
            or self._identity is not None
        ):
            raise ValueError("Bind requires the launched initial keyframe")
        self._layout.check_manifest(manifest)
        await self._control("bind", model_dir, manifest)

    async def apply(self, version_dir: Path, manifest: WeightManifest) -> None:
        if self._identity is None:
            raise RuntimeError("Native sparse resident is not bound")
        await self._control("apply", version_dir, manifest)

    async def reset_cache(self) -> None:
        if self._failed or self._identity is None:
            raise RuntimeError("Healthy bound sparse resident required")
        try:
            await self._post("/flush_cache", {}, plain=True)
        except BaseException:
            self._failed = True
            raise

    async def ready(self) -> bool:
        if self._model_dir is None or self._failed:
            return False
        try:
            async with ClientSession(timeout=ClientTimeout(total=5)) as client:
                async with client.get(self.url + "/health") as response:
                    return response.status == 200
        except (ClientError, OSError, TimeoutError):
            return False
