#!/usr/bin/env python3
"""Exercise real ChalkFS weight publication without allocating compute.

Run with chalkcompute[weights] and an authenticated CHALK_* environment. Only
this invocation's unique volume is removed, including on test failure.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import uuid
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.env_file:
        for line in args.env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key.startswith("CHALK_"):
                    os.environ[key] = value.strip().strip('"').strip("'")
    import torch
    from safetensors.torch import load_file
    from chalkcompute._volume import Volume, VolumeNotFoundError
    from chalkcompute.weights import WeightStream

    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "volume": "weight-stream-smoke-" + uuid.uuid4().hex[:12],
        "status": "running",
        "cleanup": "pending",
    }
    volume = Volume(report["volume"])
    started = time.monotonic()
    try:
        stream = WeightStream(volume.name, "fixture/cpu", volume=volume).create()
        pub = stream.publisher(config={"model_type": "fixture"}, keyframe_every=2)
        profiles = []
        state = {"fixture.weight": torch.zeros(100, dtype=torch.bfloat16)}
        assert pub.publish(state, step=0) == 0
        profiles.append(pub.last_publish_stats)
        state["fixture.weight"][7] = 1
        assert pub.publish(state, step=1) == 1
        profiles.append(pub.last_publish_stats)
        stream.pin(0)
        state["fixture.weight"][7] = 0
        assert pub.publish(state, step=2) == 2
        profiles.append(pub.last_publish_stats)
        assert stream.current() == 2 and stream.pinned() == 0
        with tempfile.TemporaryDirectory() as root:
            folder = stream.materialize(2, Path(root) / "checkpoint")
            actual = load_file(str(folder / "model.safetensors"))
            assert torch.equal(actual["fixture.weight"], state["fixture.weight"])
            pinned = stream.materialize(0, Path(root) / "pinned")
            assert (
                torch.count_nonzero(
                    load_file(str(pinned / "model.safetensors"))["fixture.weight"]
                )
                == 0
            )
        stream.unpin()
        assert stream.pinned() is None
        report.update(
            status="passed",
            versions=[m.version for m in stream.versions()],
            publication_profiles=profiles,
            elapsed_seconds=time.monotonic() - started,
        )
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        try:
            volume.delete()
            try:
                Volume(
                    volume.name, client=volume._client, create_if_missing=False
                ).reload()
            except VolumeNotFoundError:
                report["cleanup"] = "verified_deleted"
            else:
                raise RuntimeError("Test volume remains after deletion")
        except Exception:
            report["cleanup"] = "failed"
            raise
        finally:
            volume.close()
            (args.output_dir / "result.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
            print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
