"""
Upload openai/clip-vit-base-patch32 model weights into a Chalk volume.

Uploads model files with a flat layout so vLLM can load directly from the
mount path without HuggingFace cache resolution.

Run once before starting the container:

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
SNAPSHOT_DIR = (
    Path.home() / ".cache" / "huggingface" / "hub"
    / "models--openai--clip-vit-base-patch32" / "snapshots"
)

# Files vLLM doesn't need
SKIP_NAMES = {"flax_model.msgpack", "tf_model.h5", ".gitattributes", "README.md"}

CHUNK_SIZE = 20 * 1024 * 1024  # 20 MB


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

    # Find the snapshot directory (there should be exactly one)
    if not SNAPSHOT_DIR.exists():
        print(f"Model cache not found at {SNAPSHOT_DIR}")
        print("Run: uv run hf download openai/clip-vit-base-patch32")
        sys.exit(1)

    snapshot_dirs = [d for d in SNAPSHOT_DIR.iterdir() if d.is_dir()]
    if len(snapshot_dirs) != 1:
        print(f"Expected 1 snapshot, found {len(snapshot_dirs)}: {snapshot_dirs}")
        sys.exit(1)
    model_dir = snapshot_dirs[0]

    files = [
        f for f in model_dir.iterdir()
        if f.is_file() and f.name not in SKIP_NAMES
    ]
    print(f"Found {len(files)} model files in {model_dir.name}")

    print("Authenticating...")
    token = get_access_token(api_server, client_id, client_secret)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Chalk-Env-Id": env_id,
    }

    # Upload each file with a flat path (just the filename)
    # Files larger than CHUNK_SIZE are split into .part000, .part001, etc.
    print(f"Uploading to volume '{MODEL_VOLUME}'...")
    total_parts = 0
    for f in sorted(files):
        size = f.stat().st_size
        size_mb = size / 1024 / 1024
        raw = f.read_bytes()

        if size <= CHUNK_SIZE:
            print(f"  {f.name} ({size_mb:.1f} MB)...")
            data = base64.b64encode(raw).decode("ascii")
            resp = volume_rpc(api_server, headers, "PutFile", {
                "volumeName": MODEL_VOLUME,
                "path": f.name,
                "data": data,
            }, timeout=300)
            if resp.status_code != 200:
                print(f"    FAILED: {resp.text}")
                sys.exit(1)
            total_parts += 1
        else:
            num_chunks = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
            print(f"  {f.name} ({size_mb:.1f} MB) -> {num_chunks} chunks...")
            for i in range(num_chunks):
                chunk = raw[i * CHUNK_SIZE : (i + 1) * CHUNK_SIZE]
                part_name = f"{f.name}.part{i:03d}"
                chunk_mb = len(chunk) / 1024 / 1024
                print(f"    {part_name} ({chunk_mb:.1f} MB)...")
                data = base64.b64encode(chunk).decode("ascii")
                resp = volume_rpc(api_server, headers, "PutFile", {
                    "volumeName": MODEL_VOLUME,
                    "path": part_name,
                    "data": data,
                }, timeout=300)
                if resp.status_code != 200:
                    print(f"      FAILED: {resp.text}")
                    sys.exit(1)
                total_parts += 1

    print(f"\nDone. {total_parts} files/chunks uploaded to volume '{MODEL_VOLUME}'.")


if __name__ == "__main__":
    main()
