import argparse
import logging
import os
import sys
import time
import uuid

import httpx
from dotenv import load_dotenv

from chalkcompute import Container, Image

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

VLLM_PORT = 8000
MODEL = "openai/clip-vit-base-patch32"
MODEL_VOLUME = "clip-vit-base-patch32"
MODEL_DIR = "/model"  # volume mounted here with flat model files
MODEL_LOCAL_PATH = "/tmp/model"  # reassembled files go here; vLLM reads from local disk

START_SERVER_CMD = (
    f"nohup python3 -m vllm.entrypoints.openai.api_server "
    f"--model {MODEL_LOCAL_PATH} "
    f"--served-model-name {MODEL} "
    f"--host 0.0.0.0 --port {VLLM_PORT} "
    f"> /tmp/vllm.log 2>&1 &"
)

HEALTH_CHECK_CMD = (
    f"curl -sf http://localhost:{VLLM_PORT}/health > /dev/null 2>&1; echo $?"
)

DEFAULT_URLS = [
    "https://images.unsplash.com/photo-1514888286974-6c03e2ca1dba?w=320",
    "https://images.unsplash.com/photo-1587300003388-59208cc962cb?w=320",
]

EMBED_SCRIPT = f"""\
import io
import math
import sys
import urllib.request
import torch
from PIL import Image
from transformers import CLIPModel, CLIPProcessor

MODEL_PATH = "{MODEL_LOCAL_PATH}"

urls = sys.argv[1:]
if not urls:
    print("No image URLs provided.", file=sys.stderr)
    sys.exit(1)

print("Loading CLIP model...")
model = CLIPModel.from_pretrained(MODEL_PATH)
processor = CLIPProcessor.from_pretrained(MODEL_PATH)
device = "cuda" if torch.cuda.is_available() else "cpu"
model = model.to(device)
model.eval()
print(f"Model loaded on {{device}}")


def embed(image_url: str) -> list[float]:
    req = urllib.request.Request(image_url, headers={{"User-Agent": "ChalkEmbed/1.0"}})
    img_bytes = urllib.request.urlopen(req, timeout=30).read()
    image = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    inputs = processor(images=image, return_tensors="pt").to(device)
    with torch.no_grad():
        emb = model.get_image_features(**inputs)
    emb = emb / emb.norm(dim=-1, keepdim=True)
    return emb[0].cpu().tolist()


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    return dot / (mag_a * mag_b)


embeddings = []
for url in urls:
    print(f"Embedding {{url}}...")
    embeddings.append(embed(url))

dim = len(embeddings[0])
print(f"\\nEmbedding dimension: {{dim}}")
for i, (url, emb) in enumerate(zip(urls, embeddings)):
    print(f"  [{{i+1}}] {{url}}")
    print(f"      first 5 values: {{[round(x, 4) for x in emb[:5]]}}")

if len(embeddings) >= 2:
    sim = cosine(embeddings[0], embeddings[1])
    print(f"\\nCosine similarity (1 vs 2): {{sim:.4f}}")
"""


REASSEMBLE_SCRIPT = f"""\
import os, glob, shutil
src = "{MODEL_DIR}"
dst = "{MODEL_LOCAL_PATH}"
os.makedirs(dst, exist_ok=True)

# Copy non-chunk files directly to local disk
for f in os.listdir(src):
    full = os.path.join(src, f)
    if os.path.isfile(full) and ".part" not in f and ".chunkpart" not in f and ".manifest" not in f:
        print(f"Copying {{f}}...")
        shutil.copy2(full, os.path.join(dst, f))

# Reassemble chunked files to local disk
parts = sorted(glob.glob(os.path.join(src, "*.part*")))
if parts:
    groups = {{}}
    for p in parts:
        base = os.path.basename(p).rsplit(".part", 1)[0]
        groups.setdefault(base, []).append(p)
    for base, chunk_paths in sorted(groups.items()):
        print(f"Reassembling {{base}} from {{len(chunk_paths)}} chunks...")
        with open(os.path.join(dst, base), "wb") as out:
            for cp in sorted(chunk_paths):
                out.write(open(cp, "rb").read())
    print("Reassembly complete.")
else:
    print("No chunks to reassemble.")
"""


def reassemble_chunks(container: Container) -> None:
    log.info("Reassembling chunked model files...")
    result = container.exec("python3", "-c", REASSEMBLE_SCRIPT, timeout_secs=300)
    if result.stdout_text:
        print(result.stdout_text, end="")
    if result.exit_code != 0:
        raise RuntimeError(f"Reassembly failed: {result.stderr_text}")


def prepare_model(container: Container) -> None:
    reassemble_chunks(container)


def embed_images(container: Container, urls: list[str]) -> None:
    log.info("Embedding images...")
    result = container.exec(
        "python3", "-c", EMBED_SCRIPT, *urls,
        timeout_secs=300,
    )
    if result.stdout_text:
        print(result.stdout_text, end="")
    if result.stderr_text.strip():
        print(result.stderr_text, file=sys.stderr)
    if result.exit_code != 0:
        raise RuntimeError(f"Embedding script failed (exit {result.exit_code}).")


_DOCKERFILE = os.path.join(os.path.dirname(__file__), "Dockerfile")


def ensure_model_volume() -> None:
    log.info("Ensuring model weight volume '%s' exists...", MODEL_VOLUME)
    api_server = os.environ["CHALK_API_SERVER"]
    token_resp = httpx.post(
        f"{api_server}/v1/oauth/token",
        json={
            "client_id": os.environ["CHALK_CLIENT_ID"],
            "client_secret": os.environ["CHALK_CLIENT_SECRET"],
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    token_resp.raise_for_status()
    token = token_resp.json()["access_token"]
    resp = httpx.post(
        f"{api_server}/chalk.volume.v1.VolumeService/CreateVolume",
        json={"name": MODEL_VOLUME},
        headers={
            "Authorization": f"Bearer {token}",
            "X-Chalk-Env-Id": os.environ["CHALK_ENVIRONMENT_ID"],
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if resp.status_code != 409:
        resp.raise_for_status()


def main(container_name: str, urls: list[str]) -> None:
    ensure_model_volume()
    env: dict[str, str] = {}
    if client_id := os.environ.get("CHALK_CLIENT_ID"):
        env["CHALK_CLIENT_ID"] = client_id
    if client_secret := os.environ.get("CHALK_CLIENT_SECRET"):
        env["CHALK_CLIENT_SECRET"] = client_secret

    container = Container(
        image=Image.from_dockerfile(_DOCKERFILE),
        name=container_name,
        gpu="nvidia-rtx-pro-6000:1",
        memory="24Gi",
        volumes=[(MODEL_VOLUME, MODEL_DIR)],
        env=env,
    ).run()

    try:
        prepare_model(container)
        embed_images(container, urls)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        log.info("Stopping container...")
        container.stop()
        log.info("Container stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Embed images with vLLM running in a Chalk container."
    )
    parser.add_argument(
        "--url",
        action="append",
        dest="urls",
        metavar="URL",
        help="Image URL to embed. Pass multiple times for more images. Defaults to a cat + dog demo pair.",
    )
    parser.add_argument(
        "--name",
        type=str,
        default=f"vllm-embed-{uuid.uuid4().hex[:8]}",
        help="Container name. Default: vllm-embed",
    )
    args = parser.parse_args()
    main(
        container_name=args.name,
        urls=args.urls or DEFAULT_URLS,
    )
