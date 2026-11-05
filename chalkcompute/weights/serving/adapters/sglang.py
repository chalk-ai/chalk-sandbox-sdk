"""Experimental SGLang v0.5.23 dense BF16/TP1 reload baseline.

This adapter is deliberately absent from the backend runtime catalog until an
actual image/device combination passes the documented compatibility checks.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout

from chalkcompute._weights_format import WeightManifest, WeightStreamError

# Observed OCI config for the audited linux/amd64 0.5.23 runtime image
# sha256:354700f4404ab0b8dcb493bc61d5d11e7c2a91f3a7babb4efe741cb75d4b51c0.
# These identify the build or prohibit Rust source builds; none enables runtime
# cache/parallelism/kernel features. Unknown names and changed values fail closed.
_UPSTREAM_ENV = {
    "SGLANG_BUILD_COMMIT": "297e54fd105e974cdf401e5b4d20253a22c16f90",
    "SGLANG_BUILD_URL": "https://github.com/sgl-project/sglang/actions/runs/40978390925",
    "SGLANG_IMAGE_TAG": "lmsysorg/sglang:v0.5.23-runtime",
    "SGLANG_RUST_BUILD_MODE": "never",
}


def validate_config(config: bytes) -> None:
    value = json.loads(config)
    if not isinstance(value, dict) or (
        value.get("architectures") != ["LlamaForCausalLM"]
        or value.get("model_type") != "llama"
        or value.get("torch_dtype", value.get("dtype")) not in ("bfloat16", "bf16")
        or any(
            value.get(key) is not None
            for key in ("quantization_config", "compression_config", "text_config")
        )
    ):
        raise WeightStreamError(
            "Experimental SGLang requires an unquantized dense BF16 Llama checkpoint"
        )


class SGLangAdapter:
    def __init__(
        self,
        port: int = 8001,
        engine_args: list[str] | None = None,
        *,
        diagnostic_profile: str | None = None,
    ):
        self.url = f"http://127.0.0.1:{port}"
        self._model_dir: Path | None = None
        if diagnostic_profile not in (None, "deterministic-triton"):
            raise ValueError("Unsupported experimental SGLang diagnostic profile")
        self.diagnostic_profile = diagnostic_profile
        self.engine_args = list(engine_args or [])
        # A narrow allowlist also rejects argparse abbreviations, parallelism,
        # quantization, remote code, shared/derived caches and environment files.
        index = 0
        while index < len(self.engine_args):
            option, separator, value = self.engine_args[index].partition("=")
            if option not in ("--mem-fraction-static", "--context-length"):
                raise ValueError(f"Unsupported experimental SGLang option: {option}")
            if not separator:
                index += 1
                if index == len(self.engine_args):
                    raise ValueError(f"Missing SGLang option value: {option}")
                value = self.engine_args[index]
            if option == "--context-length":
                if int(value) <= 0:
                    raise ValueError("Context length must be positive")
            elif not 0 < float(value) < 1:
                raise ValueError("Memory fraction must be between zero and one")
            index += 1

    def launch_cmd(self, model_dir: Path, port: int) -> list[str]:
        validate_config((model_dir / "config.json").read_bytes())
        if version("sglang") != "0.5.23":
            raise ValueError("Experimental reload requires exactly SGLang 0.5.23")
        if any(
            key.startswith("SGLANG_") and _UPSTREAM_ENV.get(key) != value
            for key, value in os.environ.items()
        ):
            raise ValueError(
                "Experimental SGLang rejects unreviewed SGLANG_ environment overrides"
            )
        self._model_dir = model_dir
        return [
            sys.executable,
            "-m",
            "sglang.launch_server",
            "--model-path",
            str(model_dir),
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
            *(
                ["--enable-deterministic-inference", "--attention-backend", "triton"]
                if self.diagnostic_profile == "deterministic-triton"
                else []
            ),
            *self.engine_args,
        ]

    async def _post(self, path: str, body: dict, *, json_result: bool = True) -> dict:
        async with ClientSession(timeout=ClientTimeout(total=600)) as client:
            async with client.post(self.url + path, json=body) as response:
                response.raise_for_status()
                if response.status != 200:
                    raise RuntimeError(
                        "SGLang control call was not synchronously acknowledged"
                    )
                if not json_result:
                    # v0.5.23 flush_cache returns 200 only when the scheduler
                    # acknowledges success; its response body is plain text.
                    if response.status != 200:
                        raise RuntimeError("SGLang cache flush was not acknowledged")
                    await response.read()
                    return {}
                result = await response.json()
                if not isinstance(result, dict) or result.get("error"):
                    raise RuntimeError("Invalid SGLang control response")
                return result

    async def apply(self, version_dir: Path, manifest: WeightManifest) -> None:
        validate_config((version_dir / "config.json").read_bytes())
        result = await self._post("/pause_generation", {"mode": "in_place"})
        if result.get("status") != "ok":
            raise RuntimeError("SGLang did not acknowledge native pause")
        # Once pause has been requested, every exception is uncertain engine
        # state. The supervisor fails closed and restarts; never attempt rollback.
        result = await self._post(
            "/update_weights_from_disk",
            {
                "model_path": str(version_dir),
                "load_format": "safetensors",
                "weight_version": str(manifest.version),
                "is_async": False,
                "abort_all_requests": False,
                "keep_pause": True,
                "flush_cache": True,
                "recapture_cuda_graph": False,
            },
        )
        if (
            result.get("success") is not True
            or type(result.get("num_paused_requests")) is not int
            or result["num_paused_requests"] != 0
        ):
            raise RuntimeError(
                "SGLang did not acknowledge a drained, successful disk reload"
            )
        await self._post("/flush_cache", {}, json_result=False)
        # The TP1 scheduler response is only accepted with matching manager
        # identity. Never reopen admission on an old path/version acknowledgement.
        await self._verify_runtime()
        await self._verify_model(version_dir, str(manifest.version))
        result = await self._post("/continue_generation", {"torch_empty_cache": True})
        if result.get("status") != "ok":
            raise RuntimeError("SGLang did not acknowledge native resume")

    async def reset_cache(self) -> None:
        # apply() flushes while native generation remains paused.
        return None

    async def _get(self, path: str) -> dict:
        async with ClientSession(timeout=ClientTimeout(total=5)) as client:
            async with client.get(self.url + path) as response:
                if response.status != 200:
                    raise RuntimeError("SGLang readiness identity was not acknowledged")
                # server_info is about38KiB in the pinned image. Keep the
                # complete read bounded before decoding and inspect selected keys.
                try:
                    await response.content.readexactly(256 * 1024 + 1)
                except asyncio.IncompleteReadError as exc:
                    data = exc.partial
                else:
                    raise RuntimeError("SGLang readiness response exceeds256KiB")
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise RuntimeError("Invalid SGLang readiness response")
                return value

    async def _verify_model(self, model_dir: Path, weight_version: str | None) -> None:
        info = await self._get("/model_info")
        if (
            info.get("model_path") != str(model_dir)
            or info.get("load_format") != "safetensors"
        ):
            raise RuntimeError("SGLang model path/format acknowledgement differs")
        if weight_version is not None and (
            type(info.get("weight_version")) is not str
            or info["weight_version"] != weight_version
        ):
            raise RuntimeError("SGLang applied version acknowledgement differs")

    async def _verify_runtime(self) -> None:
        info = await self._get("/server_info")
        states = info.get("internal_states")
        if (
            not isinstance(states, list)
            or len(states) != 1
            or not isinstance(states[0], dict)
        ):
            raise RuntimeError(
                "SGLang requires exactly one native scheduler acknowledgement"
            )
        if type(states[0].get("world_size")) is not int or states[0]["world_size"] != 1:
            raise RuntimeError("SGLang requires native TP/PP/DP/EP1")
        expected = {
            "dtype": "bfloat16",
            "weight_cache_mode": "off",
            "disable_cuda_graph": True,
        }
        if self.diagnostic_profile == "deterministic-triton":
            expected.update(
                {
                    "enable_deterministic_inference": True,
                    "attention_backend": "triton",
                    "sampling_backend": "pytorch",
                    "disable_radix_cache": False,
                }
            )
        configs = [info, states[0]]
        if not any(
            all(
                config.get(k) == v and type(config.get(k)) is type(v)
                for k, v in expected.items()
            )
            for config in configs
        ):
            raise RuntimeError("SGLang resolved runtime profile differs")
        if any(
            k in config and (config[k] != v or type(config[k]) is not type(v))
            for config in configs
            for k, v in expected.items()
        ):
            raise RuntimeError("SGLang resolved runtime reports conflicting settings")

    async def ready(self) -> bool:
        try:
            async with ClientSession(timeout=ClientTimeout(total=5)) as client:
                async with client.get(self.url + "/health") as response:
                    if response.status != 200:
                        return False
            if self._model_dir is None:
                raise RuntimeError("SGLang readiness requires a managed model launch")
            await self._verify_runtime()
            await self._verify_model(self._model_dir, None)
            return True
        except (ClientError, OSError, TimeoutError):
            return False
