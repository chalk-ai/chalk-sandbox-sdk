"""
Smoke test: verify a GPU container starts and becomes exec-ready.

Runs a single `nvidia-smi` to confirm the pod was scheduled onto a GPU node
and exec works. No deps installed, no model downloaded — completes in < 2 min.

    CHALK_API_SERVER=... env $(grep -v '^#' .env | xargs) \
    uv run python -m pytest examples/image_embedding/test/test_gpu_smoke.py -v -s
"""

import os
import uuid

import pytest

from chalkcompute import Container

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER")

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)


@needs_staging
def test_gpu_container_exec_ready() -> None:
    """GPU container starts, gets a host, and responds to exec."""
    name = f"test-gpu-smoke-{uuid.uuid4().hex[:8]}"
    container = Container(
        image="nvidia/cuda:12.0.0-base-ubuntu20.04",
        name=name,
        gpu="1",
    )

    try:
        container.run()
        result = container.exec("nvidia-smi")
        assert result.exit_code == 0, (
            f"nvidia-smi failed (exit {result.exit_code}):\n{result.stderr_text}"
        )
        assert "NVIDIA" in result.stdout_text, (
            f"Unexpected nvidia-smi output:\n{result.stdout_text}"
        )
    finally:
        container.stop()
