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
HF_CACHE_PATH = "/root/.cache/huggingface"
MODEL_LOCAL_PATH = f"{HF_CACHE_PATH}/hub/models--openai--clip-vit-base-patch32"

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
    "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3a/Cat03.jpg/320px-Cat03.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/thumb/2/26/YellowLabradorLooking_new.jpg/320px-YellowLabradorLooking_new.jpg",
]

EMBED_SCRIPT = f"""\
import json
import base64
import math
import sys
import urllib.request

VLLM_URL = "http://localhost:{VLLM_PORT}/v1/embeddings"
MODEL = "{MODEL}"

urls = sys.argv[1:]
if not urls:
    print("No image URLs provided.", file=sys.stderr)
    sys.exit(1)


def embed(image_url: str) -> list[float]:
    img_bytes = urllib.request.urlopen(image_url, timeout=30).read()
    img_b64 = base64.b64encode(img_bytes).decode()

    payload = json.dumps({{
        "model": MODEL,
        "input": [{{
            "type": "image_url",
            "image_url": {{"url": f"data:image/jpeg;base64,{{img_b64}}"}},
        }}],
    }}).encode()

    req = urllib.request.Request(
        VLLM_URL,
        data=payload,
        headers={{"Content-Type": "application/json"}},
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
    return resp["data"][0]["embedding"]


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


def start_server(container: Container) -> None:
    log.info("Starting vLLM server (model: %s)...", MODEL)
    container.exec("bash", "-c", START_SERVER_CMD)

    log.info("Waiting for vLLM to be ready (first run downloads model weights)...")
    for attempt in range(20):
        result = container.exec("bash", "-c", HEALTH_CHECK_CMD)
        if result.stdout_text.strip() == "0":
            log.info("vLLM server is ready.")
            return
        if attempt < 19:
            time.sleep(30)
    raise RuntimeError(
        "vLLM server did not become ready after 10 minutes (20 attempts × 30s).\n"
        "Check /tmp/vllm.log inside the container for details."
    )


def embed_images(container: Container, urls: list[str]) -> None:
    log.info("Embedding images...")
    result = container.exec(
        "python3", "-c", EMBED_SCRIPT, *urls,
        timeout_secs=60 * len(urls),
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
        port=VLLM_PORT,
        volumes=[(MODEL_VOLUME, HF_CACHE_PATH)],
        env=env,
    ).run()

    try:
        start_server(container)
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
