"""Run the independent Go projector/CBOR checker using cached dependencies only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def check(*, go_binary: Path, go_protos_root: Path, vectors: Path) -> dict:
    if not (go_protos_root / "go.mod").is_file():
        raise ValueError("go_protos_root must contain the generated Go module")
    source = Path(__file__).with_name("main.go")
    with tempfile.TemporaryDirectory(prefix="volume-fingerprint-check-") as directory:
        root = Path(directory)
        shutil.copy2(source, root / "main.go")
        # This temporary module never modifies the supplied generated checkout.
        (root / "go.mod").write_text(
            "module volume-fingerprint-check\n\ngo 1.27.3\n\nrequire (\n"
            "github.com/chalk-ai/chalk-go/gen v0.0.0\n"
            "github.com/fxamacker/cbor/v2 v2.10.0\n"
            "google.golang.org/protobuf v1.36.11\n)\n"
            "replace github.com/chalk-ai/chalk-go/gen => "
            + json.dumps(str(go_protos_root))
            + "\n"
        )
        env = {**os.environ, "GOTOOLCHAIN": "local", "GOPROXY": "off", "GOSUMDB": "off"}
        result = subprocess.run(
            [str(go_binary), "run", "-mod=mod", "-p=1", ".", str(vectors)],
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode:
            raise RuntimeError(
                "Offline Go vector checker failed: " + result.stderr[-4096:]
            )
        report = json.loads(result.stdout)
        toolchain = subprocess.run(
            [str(go_binary), "version"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        report.update(
            go_toolchain=toolchain,
            generated_volume_binding_sha256=hashlib.sha256(
                (go_protos_root / "chalk/volume/v2/volume.pb.go").read_bytes()
            ).hexdigest(),
            go_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            vectors_sha256=hashlib.sha256(vectors.read_bytes()).hexdigest(),
            downloads_enabled=False,
            scope="canonical identity only; no content integrity/publication proof",
        )
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go-binary", type=Path, required=True)
    parser.add_argument("--go-protos-root", type=Path, required=True)
    parser.add_argument("--vectors", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            check(
                go_binary=args.go_binary.resolve(),
                go_protos_root=args.go_protos_root.resolve(),
                vectors=args.vectors.resolve(),
            ),
            indent=2,
        )
    )
