"""Real ScalingGroup entrypoint, optionally booting an independent audit copy."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path


def main():
    from chalkcompute._weights_format import WeightManifest, pointer_json, version_dir
    from chalkcompute.weights import WeightStream

    root = Path("/chalk/weights")
    if "CHALK_WEIGHT_AUDIT_VERSION" in os.environ:
        import xxhash

        stream = WeightStream(
            os.environ["CHALK_WEIGHT_STREAM_NAME"], os.environ["CHALK_WEIGHT_MODEL"]
        )
        checkpoint = stream.materialize(
            int(os.environ["CHALK_WEIGHT_AUDIT_VERSION"]), "/tmp/independent-checkpoint"
        )
        root = Path("/tmp/independent-stream")
        version_root = root / version_dir(0)
        version_root.mkdir(parents=True)
        for file in checkpoint.iterdir():
            if file.is_file() and file.name != "chalk-weight-version.json":
                os.link(file, version_root / file.name)
        payload = version_root / "model.safetensors"
        digest = xxhash.xxh3_128()
        with payload.open("rb") as file:
            while chunk := file.read(4 * 1024 * 1024):
                digest.update(chunk)
        original = WeightManifest.from_json(
            stream.volume.read_file(f"{version_dir(0)}/manifest.json")
        )
        entries = tuple(
            replace(
                entry,
                file=payload.name,
                offset=0,
                length=payload.stat().st_size,
                checksum=digest.hexdigest(),
            )
            for entry in original.tensors
        )
        (version_root / "manifest.json").write_bytes(
            replace(original, tensors=entries).to_json()
        )
        (root / "CURRENT").write_bytes(pointer_json(0))
    os.chmod("/dev/shm", 0o1777)
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "chalkcompute.weights.serving",
            "--weights",
            str(root),
            "--startup-timeout",
            "900",
            "--",
            "--served-model-name",
            "policy",
            "--dtype",
            "bfloat16",
            "--max-model-len",
            "2048",
            "--gpu-memory-utilization",
            ".5",
            "--enforce-eager",
        ],
    )


if __name__ == "__main__":
    main()
