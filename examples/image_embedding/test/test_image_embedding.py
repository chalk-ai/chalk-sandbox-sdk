"""
Tests for the image embedding example.

Proves that:
1. The vLLM server starts and passes its health check.
2. The /v1/embeddings endpoint returns dense vectors for image inputs.

Run locally after decrypting credentials:
    just env
    uv run python -m pytest examples/image_embedding/test/test_image_embedding.py -v
"""

import os
import time
import uuid

import pytest

from chalkcompute import Container

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER")

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)

VLLM_PORT = 8000
MODEL = "BAAI/bge-visualized"

INSTALL_SCRIPT = (
    "apt-get update -qq && "
    "apt-get install -y -qq python3 python3-pip curl 2>&1 && "
    "pip install --quiet vllm pillow 2>&1"
)

START_SERVER_CMD = (
    f"nohup python3 -m vllm.entrypoints.openai.api_server "
    f"--model {MODEL} "
    f"--host 0.0.0.0 --port {VLLM_PORT} "
    f"> /tmp/vllm.log 2>&1 &"
)

HEALTH_CHECK_CMD = (
    f"curl -sf http://localhost:{VLLM_PORT}/health > /dev/null 2>&1; echo $?"
)

EMBED_SCRIPT = f"""\
import json, base64, urllib.request

img_bytes = urllib.request.urlopen(
    "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3a/Cat03.jpg/320px-Cat03.jpg",
    timeout=30,
).read()
img_b64 = base64.b64encode(img_bytes).decode()

payload = json.dumps({{
    "model": "{MODEL}",
    "input": [{{
        "type": "image_url",
        "image_url": {{"url": f"data:image/jpeg;base64,{{img_b64}}"}},
    }}],
}}).encode()

req = urllib.request.Request(
    "http://localhost:{VLLM_PORT}/v1/embeddings",
    data=payload,
    headers={{"Content-Type": "application/json"}},
)
resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
emb = resp["data"][0]["embedding"]
print(f"dim={{len(emb)}}")
print(f"nonzero={{sum(1 for x in emb if x != 0.0)}}")
"""


@pytest.fixture(scope="module")
def container():
    """Shared container for all tests — created once, torn down after the module."""
    name = f"test-vllm-embed-{uuid.uuid4().hex[:8]}"
    c = Container(
        image="nvidia/cuda:12.1.0-devel-ubuntu22.04",
        name=name,
        gpu="1",
        memory="40Gi",
    ).run()

    # Install deps
    result = c.exec("bash", "-c", INSTALL_SCRIPT, timeout_secs=600)
    if result.exit_code != 0:
        c.stop()
        pytest.fail(f"Dependency install failed:\n{result.stdout_text.strip()}")

    # Start vLLM
    c.exec("bash", "-c", START_SERVER_CMD)

    # Wait for readiness (up to 10 min for model download + load)
    deadline = time.monotonic() + 600.0
    while time.monotonic() < deadline:
        r = c.exec("bash", "-c", HEALTH_CHECK_CMD)
        if r.stdout_text.strip() == "0":
            break
        time.sleep(10)
    else:
        logs = c.exec("bash", "-c", "cat /tmp/vllm.log 2>/dev/null || true")
        c.stop()
        pytest.fail(
            f"vLLM server never became ready within 600s.\n"
            f"vllm.log tail:\n{logs.stdout_text[-2000:]}"
        )

    yield c
    c.stop()


@needs_staging
def test_vllm_server_is_healthy(container: Container) -> None:
    """The /health endpoint returns HTTP 200 once the model is loaded."""
    result = container.exec("bash", "-c", HEALTH_CHECK_CMD)
    assert result.stdout_text.strip() == "0", (
        f"Health check failed (exit {result.exit_code}): {result.stderr_text}"
    )


@needs_staging
def test_image_embedding_returns_vectors(container: Container) -> None:
    """Embedding a JPEG image returns a dense, non-zero vector."""
    result = container.exec("python3", "-c", EMBED_SCRIPT, timeout_secs=120)
    assert result.exit_code == 0, (
        f"Embedding script failed (exit {result.exit_code}):\n{result.stderr_text}"
    )

    output = result.stdout_text
    assert "dim=" in output, f"Expected 'dim=' in output:\n{output}"

    dim_line = next(l for l in output.splitlines() if l.startswith("dim="))
    dim = int(dim_line.split("=")[1])
    assert dim > 0, f"Embedding dimension should be positive, got {dim}"

    nonzero_line = next(l for l in output.splitlines() if l.startswith("nonzero="))
    nonzero = int(nonzero_line.split("=")[1])
    assert nonzero > 0, "Embedding vector is all zeros — model likely did not run"
