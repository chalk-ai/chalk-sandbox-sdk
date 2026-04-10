"""
Upload BAAI/bge-visualized model weights from the local HuggingFace cache
into the 'vllm-model-bge-visualized' Chalk volume.

Run once before starting the container so vLLM doesn't have to download
~7 GB of weights on every cold start.

Usage:
    uv run python examples/image_embedding/upload_model_weights.py
"""

import base64
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

MODEL_VOLUME = "clip-vit-base-patch32"
# Path inside the volume (relative to the mount at /root/.cache/huggingface)
HF_HUB_DIR = Path.home() / ".cache" / "huggingface" / "hub" / "models--openai--clip-vit-base-patch32"


def get_access_token(api_server: str, client_id: str, client_secret: str) -> str:
    resp = httpx.post(
        f"{api_server}/v1/oauth/token",
        json={"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def volume_rpc(api_server: str, headers: dict, method: str, body: dict, timeout: float = 60) -> httpx.Response:
    return httpx.post(
        f"{api_server}/chalk.volume.v1.VolumeService/{method}",
        json=body,
        headers=headers,
        timeout=timeout,
    )


def main() -> None:
    api_server = os.environ["CHALK_API_SERVER"]
    client_id = os.environ["CHALK_CLIENT_ID"]
    client_secret = os.environ["CHALK_CLIENT_SECRET"]
    env_id = os.environ.get("CHALK_ENVIRONMENT_ID", "")

    if not HF_HUB_DIR.exists():
        print(f"Model cache not found at {HF_HUB_DIR}")
        print("Run: pip install huggingface_hub && huggingface-cli download BAAI/bge-visualized")
        sys.exit(1)

    print("Authenticating...")
    token = get_access_token(api_server, client_id, client_secret)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Chalk-Env-Id": env_id,
    }

    files = [f for f in HF_HUB_DIR.rglob("*") if f.is_file()]
    print(f"Found {len(files)} files to upload from {HF_HUB_DIR}")

    for i, local_path in enumerate(files, 1):
        # Path in volume is relative to /root/.cache/huggingface
        # e.g. hub/models--BAAI--bge-visualized/blobs/abc123
        rel = local_path.relative_to(Path.home() / ".cache" / "huggingface")
        volume_path = str(rel)

        size_mb = local_path.stat().st_size / 1024 / 1024
        print(f"[{i}/{len(files)}] {volume_path} ({size_mb:.1f} MB)...")

        data = base64.b64encode(local_path.read_bytes()).decode("ascii")
        resp = volume_rpc(api_server, headers, "PutFile", {
            "volumeName": MODEL_VOLUME,
            "path": volume_path,
            "data": data,
        }, timeout=120)

        if resp.status_code != 200:
            print(f"  FAILED: {resp.text}")
            sys.exit(1)

    print(f"\nDone. {len(files)} files uploaded to volume '{MODEL_VOLUME}'.")


if __name__ == "__main__":
    main()
