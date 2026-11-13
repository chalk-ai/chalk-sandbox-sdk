import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from chalkcompute.weights.serving.managed_target import ModelTargetNotSelected
from chalkcompute.weights.serving.model_runtime import (
    ManagedModelRuntime,
    ModelServingConfig,
)


def config(**overrides):
    return json.dumps(
        {
            "schema_version": 1,
            "engine": "vllm-sparse",
            "engine_args": ["--max-model-len", "256"],
            "weights_dir": "/weights",
            "weight_layout": "/layout.json",
            "weight_layout_sha256": "a" * 64,
            "engine_port": 8001,
            "startup_timeout_seconds": 1,
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"engine": "dense"},
        {"engine_port": 8080},
        {"engine_port": True},
        {"weights_dir": "relative"},
        {"weights_dir": "/"},
        {"weights_dir": "/foo/../bar"},
        {"startup_timeout_seconds": 1.5},
        {"initial_model_dir": "relative"},
        {"startup_timeout_seconds": float("nan")},
        {"weight_layout_sha256": "A" * 64},
        {"unknown": 1},
    ],
)
def test_reject_invalid_config(changes):
    with pytest.raises(ValueError):
        ModelServingConfig.from_json(config(**changes))


class Handle:
    requires_target = True

    def __init__(self):
        self.serving = None

    def set_serving(self, serving, **kwargs):
        self.serving = serving

    def wait_drained(self, timeout_seconds):
        return True


class Absent:
    def current(self):
        raise ModelTargetNotSelected("No target selected")


def test_unselected_revision_stays_closed_and_stops():
    handle = Handle()
    runtime = ManagedModelRuntime(
        ModelServingConfig.from_json(config()), handle, Absent()
    )
    runtime.start()
    assert handle.serving is False
    runtime.close()
    assert not runtime._thread.is_alive()
    assert runtime.failure is None


@pytest.mark.parametrize("fail_after_launch", [False, True])
def test_runtime_owns_engine_process_and_cleanup(
    monkeypatch, tmp_path, fail_after_launch
):
    from chalkcompute.weights.serving import model_runtime
    from chalkcompute.weights.serving.adapters import vllm_resident

    pid_file = tmp_path / "pid"
    child_started = threading.Event()

    class Adapter:
        def __init__(self, *args, **kwargs):
            pass

        def launch_cmd(self, directory, port):
            return [
                sys.executable,
                "-c",
                "import os,time; from pathlib import Path; Path("
                + repr(str(pid_file))
                + ").write_text(str(os.getpid())); time.sleep(60)",
            ]

        async def ready(self):
            ready = pid_file.exists()
            if ready:
                child_started.set()
            return ready

    class Syncer:
        def __init__(self, root, adapter, gate, **kwargs):
            pass

        async def bootstrap(self, launch, initial_model_dir=None):
            await launch(Path("/initial"))
            if fail_after_launch:
                raise ValueError("bad resident seed")

        async def run(self):
            await asyncio.Future()

    class Provider:
        def current(self):
            return object()

    monkeypatch.setattr(vllm_resident, "VLLMResidentAdapter", Adapter)
    monkeypatch.setattr(model_runtime, "SparseWeightSyncer", Syncer)
    handle = Handle()
    runtime = ManagedModelRuntime(
        ModelServingConfig.from_json(config()), handle, Provider()
    )
    runtime.start()
    assert child_started.wait(5)
    pid = int(pid_file.read_text())
    if fail_after_launch:
        runtime._thread.join(5)
        assert isinstance(runtime.failure, ValueError)
    runtime.close()
    assert handle.serving is False
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not runtime._thread.is_alive()


def test_empty_engine_args_are_valid_but_missing_or_duplicate_fields_are_not():
    assert ModelServingConfig.from_json(config(engine_args=[])).engine_args == []
    value = json.loads(config())
    del value["engine_args"]
    with pytest.raises(ValueError):
        ModelServingConfig.from_json(json.dumps(value))
    with pytest.raises(ValueError, match="Duplicate"):
        ModelServingConfig.from_json(config()[:-1] + ', "engine_port": 9001}')


def test_target_authentication_failure_is_terminal():
    from chalkcompute.weights.serving.managed_target import ManagedTargetError

    class Unauthorized:
        def current(self):
            raise ManagedTargetError("Authentication rejected")

    handle = Handle()
    runtime = ManagedModelRuntime(
        ModelServingConfig.from_json(config()), handle, Unauthorized()
    )
    try:
        runtime.start()
    except RuntimeError:
        assert isinstance(runtime.failure, ManagedTargetError)
    runtime._thread.join(3)
    assert isinstance(runtime.failure, ManagedTargetError)
    assert not runtime._thread.is_alive()
    assert handle.serving is False
    runtime.close()
