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
            "engine": "SERVING_ENGINE_VLLM",
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


def bootstrap(**overrides):
    return json.dumps(
        {
            "schema_version": 1,
            "config": json.loads(config()),
            "binding": {
                "environment_id": "environment",
                "weight_stream_id": "stream",
                "volume_id": "volume",
            },
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"target": {}},
        {"lease": 12},
        {"config": None},
        {"binding": None},
    ],
)
def test_bootstrap_is_strict_and_contains_only_static_configuration(changes):
    from chalkcompute.weights.serving.model_runtime import ModelServingBootstrap

    with pytest.raises(ValueError):
        ModelServingBootstrap.from_json(bootstrap(**changes))


@pytest.mark.parametrize(
    "field",
    [
        "environment_id",
        "weight_stream_id",
        "volume_id",
    ],
)
@pytest.mark.parametrize("invalid", [None, True, 1, "", " spaced ", "bad\x00identity"])
def test_bootstrap_binding_requires_exact_authoritative_identities(field, invalid):
    from chalkcompute.weights.serving.model_runtime import ModelServingBootstrap

    value = json.loads(bootstrap())
    value["binding"][field] = invalid
    with pytest.raises(ValueError, match="binding"):
        ModelServingBootstrap.from_json(json.dumps(value))


def test_bootstrap_uses_whole_utf8_bound_and_rejects_nested_duplicates():
    from chalkcompute.weights.serving.model_runtime import ModelServingBootstrap

    raw = bootstrap()
    parsed = ModelServingBootstrap.from_json(
        raw + " " * (64 * 1024 - len(raw.encode()))
    )
    assert parsed.config.engine == "vllm-sparse"
    with pytest.raises(ValueError, match="size bound"):
        ModelServingBootstrap.from_json(raw + " " * (64 * 1024 - len(raw.encode()) + 1))
    value = json.loads(raw)
    value["binding"]["environment_id"] = "é" * (32 * 1024)
    with pytest.raises(ValueError, match="size bound"):
        ModelServingBootstrap.from_json(json.dumps(value, ensure_ascii=False))
    with pytest.raises(ValueError, match="Duplicate"):
        ModelServingBootstrap.from_json(
            raw.replace(
                '"engine_port": 8001', '"engine_port": 8001, "engine_port": 8002'
            )
        )
    with pytest.raises(ValueError, match="Duplicate"):
        ModelServingBootstrap.from_json(
            raw.replace(
                '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
            )
        )


@pytest.mark.parametrize("legacy_present", [False, True])
def test_typed_bootstrap_factory_ignores_legacy_mode_and_identity_env(
    monkeypatch, legacy_present, tmp_path
):
    from chalkcompute.weights.serving import model_target

    captured = {}
    monkeypatch.delenv("CHALK_MODEL_ENGINE_URL", raising=False)

    def provider(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(model_target, "ModelDeploymentTargetProvider", provider)
    monkeypatch.setenv("CHALK_API_SERVER", "https://api.example.test")
    token_file = tmp_path / "identity-token"
    metadata = tmp_path / "scaling-group-metadata"
    (tmp_path / "scaling-group-metadata").write_text(
        'chalk.ai/scaling-group="deployment"\nchalk.ai/scaling-group-revision="revision"'
    )
    monkeypatch.setenv("CHALK_FUNCTION_QUEUE_TOKEN_FILE", str(token_file))
    opened = []
    original_open = Path.open

    def track_open(path, *args, **kwargs):
        if path == metadata:
            opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", track_open)
    for name in [
        "CHALK_MODEL_SERVING_CONFIG",
        "CHALK_MODEL_SCALING_GROUP_ID",
        "CHALK_MODEL_SCALING_GROUP_REVISION_ID",
        "CHALK_WEIGHT_STREAM_ID",
        "CHALK_WEIGHT_VOLUME_ID",
        "CHALK_ENVIRONMENT_ID",
    ]:
        if legacy_present:
            monkeypatch.setenv(name, "untrusted-legacy-value")
        else:
            monkeypatch.delenv(name, raising=False)
    runtime = ManagedModelRuntime.from_bootstrap(bootstrap(), Handle())
    assert runtime.config.engine == "vllm-sparse"
    assert opened == [metadata]
    (tmp_path / "scaling-group-metadata").write_text(
        'chalk.ai/scaling-group="deployment"\nchalk.ai/scaling-group-revision="new-revision"'
    )
    assert captured == {
        "api_server": "https://api.example.test",
        "token_file": token_file,
        "expected_environment_id": "environment",
        "expected_deployment_id": "deployment",
        "expected_revision_id": "revision",
        "expected_stream_id": "stream",
        "expected_volume_id": "volume",
    }
    assert os.environ["CHALK_MODEL_ENGINE_URL"] == "http://127.0.0.1:8001"
    monkeypatch.delenv("CHALK_FUNCTION_QUEUE_TOKEN_FILE")
    with pytest.raises(ValueError, match="platform FNQ"):
        ManagedModelRuntime.from_bootstrap(bootstrap(), Handle())


@pytest.mark.parametrize(
    "engine, expected",
    [
        ("SERVING_ENGINE_VLLM", "vllm-sparse"),
        ("SERVING_ENGINE_SGLANG", "sglang-sparse"),
    ],
)
def test_bootstrap_enum_names_select_existing_sparse_runtime(engine, expected):
    from chalkcompute.weights.serving.model_runtime import ModelServingBootstrap

    value = json.loads(bootstrap())
    value["config"]["engine"] = engine
    value["config"]["engine_args"] = []
    assert ModelServingBootstrap.from_json(json.dumps(value)).config.engine == expected


def test_portable_bootstrap_fixture():
    from chalkcompute.weights.serving.model_runtime import ModelServingBootstrap

    raw = (Path(__file__).parent / "fixtures/model_serving_bootstrap.json").read_text()
    parsed = ModelServingBootstrap.from_json(raw)
    assert parsed.config.engine_args == []
    assert parsed.config.initial_model_dir == Path("/snapshot/model")
    assert parsed.binding.weight_stream_id == "stream"
    assert not hasattr(parsed.binding, "model_scaling_group_revision_id")


@pytest.mark.parametrize("platform", ["host", "kubernetes"])
def test_replica_metadata_platform_files(tmp_path, platform):
    from chalkcompute.weights.serving.model_runtime import ModelServingReplica

    fixture = Path(__file__).parent / f"fixtures/model_serving_metadata_{platform}.txt"
    (tmp_path / "scaling-group-metadata").write_bytes(fixture.read_bytes())
    captured = ModelServingReplica.read(tmp_path / "identity-token")
    assert captured.deployment_id == "group-A_1.2"
    assert captured.revision_id == "12345678-1234-1234-1234-123456789abc"


@pytest.mark.parametrize(
    "entry",
    [
        "",
        '=""',
        '=" spaced "',
        '="bad\x00id"',
        '="bad\\nid"',
        '="bad\\u0061id"',
        '="bad/id"',
        '="bad:id"',
        '="-bad"',
        '="bad."',
        '="' + "x" * 64 + '"',
        "=unquoted",
        '="unterminated',
        '="valid" trailing',
        ' ="valid"',
        ' "valid"',
        ':"valid"',
        '="valid"\r',
    ],
)
@pytest.mark.parametrize(
    "key", ["chalk.ai/scaling-group", "chalk.ai/scaling-group-revision"]
)
def test_replica_metadata_rejects_invalid_required_entry(tmp_path, key, entry):
    from chalkcompute.weights.serving.model_runtime import ModelServingReplica

    other = (
        "chalk.ai/scaling-group-revision"
        if key == "chalk.ai/scaling-group"
        else "chalk.ai/scaling-group"
    )
    (tmp_path / "scaling-group-metadata").write_text(f'{other}="valid"\n{key}{entry}')
    with pytest.raises(ValueError, match="replica metadata"):
        ModelServingReplica.read(tmp_path / "identity-token")


@pytest.mark.parametrize(
    "raw",
    [
        None,
        b"",
        b'other="label"',
        b'chalk.ai/scaling-group="deployment"',
        b'chalk.ai/scaling-group-revision="revision"',
        b'chalk.ai/scaling-group="deployment"\nchalk.ai/scaling-group="deployment"\nchalk.ai/scaling-group-revision="revision"',
        b'chalk.ai/scaling-group="deployment"\nchalk.ai/scaling-group-revision="revision"\nchalk.ai/scaling-group-revision="different"',
        b'chalk.ai/scaling-group="deployment"\nchalk.ai/scaling-group-revision="revision"\nother="\xff"',
        b'chalk.ai/scaling-group="deployment"\nchalk.ai/scaling-group-revision="revision"\n'
        + b"x" * (64 * 1024),
    ],
)
def test_replica_metadata_is_required_and_bounded(tmp_path, raw):
    from chalkcompute.weights.serving.model_runtime import ModelServingReplica

    # Old two-file metadata never supplies a fallback identity.
    (tmp_path / "scaling-group-id").write_text("deployment")
    (tmp_path / "scaling-group-revision-id").write_text("revision")
    if raw is not None:
        (tmp_path / "scaling-group-metadata").write_bytes(raw)
    with pytest.raises(ValueError, match="replica metadata"):
        ModelServingReplica.read(tmp_path / "identity-token")


def test_replica_metadata_accepts_label_bounds_and_exact_file_bound(tmp_path):
    from chalkcompute.weights.serving.model_runtime import ModelServingReplica

    text = (
        'chalk.ai/scaling-group="'
        + "x" * 63
        + '"\nchalk.ai/scaling-group-revision="A"\n'
    )
    text += "unrelated=" + "x" * (64 * 1024 - len(text) - len("unrelated="))
    (tmp_path / "scaling-group-metadata").write_text(text)
    assert (
        ModelServingReplica.read(tmp_path / "identity-token").deployment_id == "x" * 63
    )


def test_running_replica_never_adopts_a_new_selected_revision(tmp_path):
    from chalkcompute.weights.serving.model_runtime import ModelServingReplica

    metadata = tmp_path / "scaling-group-metadata"
    metadata.write_text(
        'chalk.ai/scaling-group="deployment"\nchalk.ai/scaling-group-revision="A"'
    )
    captured = ModelServingReplica.read(tmp_path / "identity-token")
    metadata.write_text(
        'chalk.ai/scaling-group="deployment"\nchalk.ai/scaling-group-revision="B"'
    )
    assert captured.revision_id == "A"
    assert ModelServingReplica.read(tmp_path / "identity-token").revision_id == "B"


def test_bootstrap_cannot_supply_replica_identity():
    from chalkcompute.weights.serving.model_runtime import ModelServingBootstrap

    value = json.loads(bootstrap())
    value["binding"]["model_scaling_group_revision_id"] = "B"
    with pytest.raises(ValueError, match="binding"):
        ModelServingBootstrap.from_json(json.dumps(value))
