#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["chalkcompute>=2.11.1", "python-dotenv"]
# ///
"""
Deploy Gemma 3 4B on an L4 GPU via vLLM, served as an OpenAI-compatible API.

This example intentionally leaves the named scaling group running: `chat.py`
and other clients need the service after this deployment process exits. It also
leaves failures after deployment running so their logs and state can be examined.
The group reserves a GPU while it remains deployed. Remove it through the Chalk
SDK or dashboard when finished.

Prerequisites:
    export HF_TOKEN=hf_...  # huggingface.co token with Gemma access
"""

from __future__ import annotations

import json
import sys
import time

from chalkcompute import ScalingGroup, Secret
from dotenv import load_dotenv

load_dotenv()

# -- Config -------------------------------------------------------------------

MODEL = "google/gemma-3-4b-it"
PORT = 8000

# -- Image + ScalingGroup ----------------------------------------------------

sg = ScalingGroup(
    image="vllm/vllm-openai:latest",
    name="gemma-vllm",
    gpu="nvidia-l4",
    cpu="4",
    memory="16Gi",
    port=PORT,
    entrypoint=[
        "python3",
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        MODEL,
        "--port",
        str(PORT),
        "--trust-remote-code",
        "--max-model-len",
        "4096",
        "--dtype",
        "bfloat16",
        "--gpu-memory-utilization",
        "0.90",
        "--enforce-eager",
    ],
    env={"HF_HOME": "/root/.cache/huggingface"},
    secrets=[Secret.from_local_env("HF_TOKEN")],
)

# -- Deploy and wait for vLLM health -----------------------------------------

print("Deploying scaling group (image build + GPU provisioning)...")
sg.deploy(build_timeout=600.0, ready_timeout=600.0)
print(f"Scaling group running: {sg.web_url}")

print("Waiting for vLLM to load the model...")
for _ in range(60):
    try:
        resp = sg.call("/health", method="GET", timeout=10)
        if resp.status == 200:
            break
    except Exception:
        pass
    time.sleep(15)
else:
    print("Timed out waiting for vLLM health check.")
    sys.exit(1)

# -- Test request -------------------------------------------------------------

print("Sending test completion...")
for attempt in range(10):
    resp = sg.call(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "Say hello in one sentence."}],
            "max_tokens": 32,
        },
        timeout=120,
    )
    if resp.status == 200:
        break
    print(f"  attempt {attempt + 1}: {resp.status}, retrying...")
    time.sleep(15)
if not 200 <= resp.status < 300:
    raise RuntimeError(f"Completion request failed with status {resp.status}")
response_body = json.loads(resp.content)
print(f"Response: {response_body['choices'][0]['message']['content']}")

print(f"\nServing at: {sg.web_url}")
print("Scaling group left running for chat clients and inspection.")
