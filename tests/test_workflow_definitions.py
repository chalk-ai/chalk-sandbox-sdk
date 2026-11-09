from __future__ import annotations

import importlib.util
import sys
import textwrap
import types
from pathlib import Path
from typing import Any

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError

import chalkcompute._workflow_worker as workflow_worker_mod
from chalkcompute._gen.chalk.server.v1 import workflow_orchestrator_pb as wo_pb
from chalkcompute._scaling_group import ScalingGroupInfo
from chalkcompute._workflow_definitions import (
    collect_definition_specs,
    definition_specs,
    register_workflow_definitions,
)
from tests._fakes import FakeMethod, StubbedConnectClient

pytest.importorskip("chalk.workflows")

_HELPERS = textwrap.dedent(
    """\
    from chalk.workflows import task


    @task(timeout="90s")
    def shared_lookup(key: str) -> str:
        return key
    """
)

_FLOWS = textwrap.dedent(
    '''\
    import asyncio

    from chalk.workflows import task, workflow

    from {helpers} import shared_lookup


    @task(retries=2, timeout="5m", retry_delay="1500ms")
    def build_training_data() -> str:
        """Materialize the training dataset."""
        return "ds"


    @task
    def score(row: int) -> int:
        return row


    @workflow(owner="ml@chalk.ai")
    async def fraud_retrain() -> str:
        await build_training_data()
        await asyncio.gather(*[score.submit(i) for i in range(3)])
        return await shared_lookup("k")


    @workflow(description="Does no work.")
    async def idle() -> None:
        return None
    '''
)


def _import(path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def flows(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    """Write and import a workflow file plus a helper module it takes a task from."""
    from chalk.workflows import TASK_REGISTRY, WORKFLOW_REGISTRY

    suffix = request.node.name.replace("[", "_").replace("]", "_").replace("-", "_")
    helpers = tmp_path / f"helpers_{suffix}.py"
    helpers.write_text(_HELPERS)
    source = tmp_path / f"flows_{suffix}.py"
    source.write_text(_FLOWS.format(helpers=helpers.stem))
    sys.path.insert(0, str(tmp_path))
    before = (dict(TASK_REGISTRY), dict(WORKFLOW_REGISTRY))
    try:
        yield source
    finally:
        sys.path.remove(str(tmp_path))
        for name in (helpers.stem, source.stem):
            sys.modules.pop(name, None)
        TASK_REGISTRY.clear()
        TASK_REGISTRY.update(before[0])
        WORKFLOW_REGISTRY.clear()
        WORKFLOW_REGISTRY.update(before[1])


def _by_name(
    specs: list[wo_pb.WorkflowDefinitionSpec],
) -> dict[str, wo_pb.WorkflowDefinitionSpec]:
    return {spec.name: spec for spec in specs}


def test_definition_specs_describe_the_files_workflows_and_their_tasks(
    flows: Path,
) -> None:
    from chalk.workflows import TASK_REGISTRY, WORKFLOW_REGISTRY

    _import(flows)
    specs = _by_name(
        definition_specs(WORKFLOW_REGISTRY.values(), TASK_REGISTRY.values(), str(flows))
    )

    assert set(specs) == {
        "fraud_retrain",
        "idle",
        "build_training_data",
        "score",
        "shared_lookup",
    }

    retrain = specs["fraud_retrain"]
    assert retrain.kind == wo_pb.WorkflowDefinitionKind.WORKFLOW
    assert retrain.owner == "ml@chalk.ai"
    # Found in the body and inside the list comprehension.
    assert retrain.task_names == ["build_training_data", "score", "shared_lookup"]
    assert retrain.source is not None
    assert retrain.source.file_name == flows.name
    assert retrain.source.qualname == "fraud_retrain"
    assert retrain.source.code.startswith(
        '@workflow(owner="ml@chalk.ai")\nasync def fraud_retrain()'
    )
    assert (
        retrain.source.first_line
        == _FLOWS.splitlines().index('@workflow(owner="ml@chalk.ai")') + 1
    )
    assert retrain.timeout is None and retrain.retries == 0

    assert specs["idle"].description == "Does no work."
    assert specs["idle"].task_names == []

    build = specs["build_training_data"]
    assert build.kind == wo_pb.WorkflowDefinitionKind.TASK
    assert build.description == "Materialize the training dataset."
    assert build.retries == 2
    assert build.timeout is not None and (
        build.timeout.seconds,
        build.timeout.nanos,
    ) == (300, 0)
    assert build.retry_delay is not None and (
        build.retry_delay.seconds,
        build.retry_delay.nanos,
    ) == (1, 500_000_000)

    # A task from another file is described because a workflow here names it.
    shared = specs["shared_lookup"]
    assert shared.source is not None and shared.source.file_name.startswith("helpers_")
    assert shared.timeout is not None and shared.timeout.seconds == 90


def test_definition_specs_skip_other_files_definitions(
    flows: Path, tmp_path: Path
) -> None:
    from chalk.workflows import TASK_REGISTRY, WORKFLOW_REGISTRY

    _import(flows)
    specs = definition_specs(
        WORKFLOW_REGISTRY.values(), TASK_REGISTRY.values(), str(tmp_path / "other.py")
    )
    assert specs == []


def test_collect_definition_specs_uses_an_already_imported_file(
    flows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _import(flows)
    # Importing again would fail the test: the file is already registered.
    monkeypatch.setitem(sys.modules, "chalk.workflows.worker", types.SimpleNamespace())

    names = {spec.name for spec in collect_definition_specs(str(flows))}
    assert "fraud_retrain" in names


def test_collect_definition_specs_imports_a_file_that_is_not_loaded(
    flows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded: list[str] = []

    def load_workflow_source(path: str) -> str:
        loaded.append(path)
        return _import(Path(path)).__name__

    monkeypatch.setitem(
        sys.modules,
        "chalk.workflows.worker",
        types.SimpleNamespace(load_workflow_source=load_workflow_source),
    )

    names = {spec.name for spec in collect_definition_specs(str(flows))}
    assert loaded == [str(flows)]
    assert {"fraud_retrain", "idle"} <= names


class _RegisterClient(StubbedConnectClient):
    def __init__(self, error: ConnectRpcError | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.error = error

    def call(self, method: FakeMethod, request: Any, **_: Any) -> Any:
        self.calls.append((method.name, request))
        if self.error is not None:
            raise self.error
        return wo_pb.RegisterWorkflowDefinitionsResponse()


def test_register_workflow_definitions_sends_the_revision() -> None:
    client = _RegisterClient()
    specs = [
        wo_pb.WorkflowDefinitionSpec(
            kind=wo_pb.WorkflowDefinitionKind.WORKFLOW, name="idle"
        )
    ]

    assert register_workflow_definitions("wf-flows", "revision-2", specs, client=client)  # type: ignore[arg-type]

    ((method, request),) = client.calls
    assert method == "register_workflow_definitions"
    assert request.scaling_group_name == "wf-flows"
    assert request.scaling_group_revision_id == "revision-2"
    assert [d.name for d in request.definitions] == ["idle"]


def test_register_workflow_definitions_logs_instead_of_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _RegisterClient(ConnectRpcError(Code.UNIMPLEMENTED, "unknown method"))

    assert not register_workflow_definitions(
        "wf-flows", "revision-2", [], client=client
    )  # type: ignore[arg-type]
    assert "still serves them" in caplog.text


class _DeployedGroup:
    def __init__(self, revision_id: str | None) -> None:
        self.info = ScalingGroupInfo(
            id="group-1",
            name="wf-flows",
            status="Running",
            current_revision_id=revision_id,
        )


def test_deploy_registers_the_new_revisions_definitions(
    flows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _import(flows)
    registered: list[tuple[str, str, list[str]]] = []
    monkeypatch.setattr(
        workflow_worker_mod,
        "register_workflow_definitions",
        lambda name, revision, specs, **_: registered.append(
            (name, revision, [s.name for s in specs])
        ),
    )

    workflow_worker_mod._register_definitions(
        _DeployedGroup("revision-2"), str(flows), None
    )  # type: ignore[arg-type]
    workflow_worker_mod._register_definitions(_DeployedGroup(None), str(flows), None)  # type: ignore[arg-type]

    ((name, revision, names),) = registered
    assert (name, revision) == ("wf-flows", "revision-2")
    assert {"fraud_retrain", "idle"} <= set(names)


def test_deploy_survives_a_file_that_fails_to_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    broken = tmp_path / "broken_flows.py"
    broken.write_text("raise RuntimeError('boom')\n")

    def load_workflow_source(path: str) -> str:
        return _import(Path(path)).__name__

    monkeypatch.setitem(
        sys.modules,
        "chalk.workflows.worker",
        types.SimpleNamespace(load_workflow_source=load_workflow_source),
    )
    monkeypatch.setattr(
        workflow_worker_mod,
        "register_workflow_definitions",
        lambda *a, **k: pytest.fail("nothing should be registered"),
    )

    workflow_worker_mod._register_definitions(
        _DeployedGroup("revision-2"), str(broken), None
    )  # type: ignore[arg-type]
    sys.modules.pop("broken_flows", None)
    assert "Could not read the workflow definitions" in caplog.text
