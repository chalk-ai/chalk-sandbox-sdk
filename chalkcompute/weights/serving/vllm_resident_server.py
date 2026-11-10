"""Set bridge requirements before importing vLLM's official HTTP server."""

from __future__ import annotations

import os
import runpy


def main() -> None:
    from chalkcompute.weights.serving.adapters.vllm_resident import validate_environment
    from chalkcompute.weights.serving.resident_runtime import check_runtime_sources

    validate_environment()
    check_runtime_sources("vllm")
    # The bridge uses dev-mode worker RPC/cache controls on a loopback server.
    os.environ["VLLM_SERVER_DEV_MODE"] = "1"
    # Start CUDA workers without inheriting initialized state through fork.
    os.environ["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"
    runpy.run_module("vllm.entrypoints.openai.api_server", run_name="__main__")


if __name__ == "__main__":
    main()
