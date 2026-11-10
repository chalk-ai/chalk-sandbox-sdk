"""Layout admission remains usable without importing Torch or native runtimes."""

import hashlib
import subprocess
import sys

import pytest

from chalkcompute.weights.layout import TensorSpec, WeightLayout, WeightSchema
from chalkcompute.weights.serving.adapters.resident_control import LayoutSnapshot


def test_layout_snapshot_is_lazy_in_fresh_process(tmp_path):
    layout = WeightLayout().bind(
        WeightSchema((TensorSpec("custom.weight", (8,), "bfloat16"),))
    )
    path = tmp_path / "layout.json"
    raw = layout.to_json()
    path.write_bytes(raw)
    program = (
        "import sys; "
        "from pathlib import Path; "
        "import chalkcompute.weights.serving.adapters.vllm_resident; "
        "from chalkcompute.weights.serving.adapters.resident_control import LayoutSnapshot; "
        "snapshot=LayoutSnapshot(Path(sys.argv[1]),sys.argv[2]); "
        "assert not any(name in sys.modules for name in ('torch','vllm','sglang','zmq','fastapi'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, str(path), hashlib.sha256(raw).hexdigest()],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("engine", ["vllm-sparse"])
@pytest.mark.parametrize(
    "extra",
    [[], ["--weight-layout", "layout.json"], ["--weight-layout-sha256", "a" * 64]],
)
def test_sparse_cli_requires_both_layout_pins(monkeypatch, engine, extra):
    from chalkcompute.weights.serving.__main__ import main

    monkeypatch.setattr(
        sys,
        "argv",
        ["chalk-live-serve", "--engine", engine, "--weights", "/weights", *extra],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_layout_snapshot_retains_explicit_raw_pin_and_canonical_identity(tmp_path):
    layout = WeightLayout().bind(
        WeightSchema((TensorSpec("custom.weight", (8,), "bfloat16"),))
    )
    path = tmp_path / "layout.json"
    raw = b" \n" + layout.to_json() + b"\n"
    path.write_bytes(raw)
    snapshot = LayoutSnapshot(path, hashlib.sha256(raw).hexdigest())
    path.write_text("later mutation")
    assert snapshot.raw == raw
    assert snapshot.file_sha256 != snapshot.layout_sha256
    assert snapshot.layout_sha256 == layout.sha256
    assert snapshot.schema_sha256 == layout.source_schema_sha256
