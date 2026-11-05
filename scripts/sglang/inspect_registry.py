"""Read public Docker Hub manifests/config only; never request image layers."""

from __future__ import annotations
import argparse
import hashlib
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

LIMIT = 4 * 1024 * 1024
ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def get(url, headers=None):
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers or {}), timeout=30
    ) as response:
        raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("Metadata exceeds 4 MiB safety bound")
        return raw, dict(response.headers)


def inspect(repo, tag, out):
    token = json.loads(
        get(
            "https://auth.docker.io/token?"
            + urllib.parse.urlencode(
                {
                    "service": "registry.docker.io",
                    "scope": "repository:" + repo + ":pull",
                }
            )
        )[0]
    )["token"]
    headers = {"Authorization": "Bearer " + token, "Accept": ACCEPT}
    base = "https://registry-1.docker.io/v2/" + repo
    raw, response_headers = get(base + "/manifests/" + tag, headers)
    index_raw = raw
    index = json.loads(raw)
    top_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if "manifests" in index:
        matches = [
            m
            for m in index["manifests"]
            if m.get("platform", {}).get("os") == "linux"
            and m.get("platform", {}).get("architecture") == "amd64"
        ]
        if len(matches) != 1:
            raise ValueError("Require one linux/amd64 image")
        digest = matches[0]["digest"]
        raw, _ = get(base + "/manifests/" + digest, headers)
        if digest != "sha256:" + hashlib.sha256(raw).hexdigest():
            raise ValueError("Manifest digest differs")
        manifest = json.loads(raw)
    else:
        digest, manifest = top_digest, index
    config_desc = manifest["config"]
    # This is the only blob requested: the small OCI config descriptor.
    if config_desc.get("size", LIMIT + 1) > LIMIT:
        raise ValueError("Config too large")
    config_raw, _ = get(base + "/blobs/" + config_desc["digest"], headers)
    if config_desc["digest"] != "sha256:" + hashlib.sha256(config_raw).hexdigest():
        raise ValueError("Config digest differs")
    config = json.loads(config_raw)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_bytes(index_raw)
    (out / "manifest.json").write_bytes(raw)
    (out / "config.json").write_bytes(config_raw)
    history = [x.get("created_by", "") for x in config.get("history", [])]
    summary = {
        "reference": repo + ":" + tag,
        "index_digest": top_digest,
        "linux_amd64_digest": digest,
        "config_digest": config_desc["digest"],
        "created": config.get("created"),
        "architecture": config.get("architecture"),
        "os": config.get("os"),
        "compressed_layer_bytes": sum(x["size"] for x in manifest.get("layers", [])),
        "layers_requested": 0,
        "labels": config.get("config", {}).get("Labels"),
        "env": config.get("config", {}).get("Env"),
        "version_history": [
            x
            for x in history
            if re.search(
                r"ARG (?:SGL_VERSION|CUDA_VERSION|ROCM|PYTHON_VERSION)|torch==|TORCH_VERSION=|git clone.*sglang|pip install.*sglang|uv pip install.*sglang|SGL_BRANCH=",
                x,
            )
        ],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(
        json.dumps(
            {
                k: v
                for k, v in summary.items()
                if k not in ("env", "labels", "version_history")
            },
            indent=2,
        )
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("tag")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    inspect(args.repo, args.tag, args.out)
