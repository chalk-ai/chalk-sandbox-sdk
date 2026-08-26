from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Any

import pytest

from chalkcompute._function import (
    FunctionBuildError,
    FunctionVersionInfo,
    RemoteFunction,
    function,
)


def _install_fake_remote_call(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    monkeypatch.setattr(
        RemoteFunction,
        "_serialize_call_args",
        lambda self, *args, **kwargs: b"request",
    )
    monkeypatch.setattr(
        RemoteFunction,
        "_call_direct_chunks",
        lambda self, request: [b"response"],
    )
    monkeypatch.setattr(
        RemoteFunction,
        "_decode_result_chunks",
        lambda self, chunks: [result],
    )


def test_decorated_functions_start_deploying_concurrently(monkeypatch) -> None:
    rendezvous = threading.Barrier(2)
    deployed: list[str] = []
    deployed_lock = threading.Lock()

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        rendezvous.wait(timeout=5)
        with deployed_lock:
            deployed.append(self._func_name)
        return self

    monkeypatch.setattr(RemoteFunction, "_deploy", fake_deploy)

    @function
    def first(value: int) -> int:
        return value

    @function
    def second(value: int) -> int:
        return value

    first._wait_for_background_deploy()
    second._wait_for_background_deploy()

    assert set(deployed) == {"first", "second"}


def test_deployments_for_the_same_registered_name_stay_ordered(monkeypatch) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        if self._func_name == "first":
            first_started.set()
            assert release_first.wait(timeout=5)
        else:
            second_started.set()
        return self

    monkeypatch.setattr(RemoteFunction, "_deploy", fake_deploy)

    @function(name="shared-name")
    def first(value: int) -> int:
        return value

    assert first_started.wait(timeout=5)

    @function(name="shared-name")
    def second(value: int) -> int:
        return value

    assert not second_started.is_set()
    release_first.set()
    first._wait_for_background_deploy()
    second._wait_for_background_deploy()
    assert second_started.is_set()


def test_remote_call_waits_for_background_deployment(monkeypatch) -> None:
    deploy_started = threading.Event()
    release_deploy = threading.Event()
    call_finished = threading.Event()
    result: list[int] = []

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        deploy_started.set()
        assert release_deploy.wait(timeout=5)
        return self

    monkeypatch.setattr(RemoteFunction, "_deploy", fake_deploy)
    _install_fake_remote_call(monkeypatch, 42)

    @function
    def add_one(value: int) -> int:
        return value + 1

    assert deploy_started.wait(timeout=5)

    def call() -> None:
        result.append(add_one.remote(41))
        call_finished.set()

    caller = threading.Thread(target=call)
    caller.start()
    assert not call_finished.wait(timeout=0.05)
    release_deploy.set()
    caller.join(timeout=5)

    assert not caller.is_alive()
    assert result == [42]


def test_async_remote_call_does_not_block_the_event_loop(monkeypatch) -> None:
    deploy_started = threading.Event()
    release_deploy = threading.Event()

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        deploy_started.set()
        assert release_deploy.wait(timeout=5)
        return self

    monkeypatch.setattr(RemoteFunction, "_deploy", fake_deploy)
    _install_fake_remote_call(monkeypatch, 42)

    @function
    async def add_one(value: int) -> int:
        return value + 1

    assert deploy_started.wait(timeout=5)

    async def run() -> None:
        call_task = asyncio.create_task(add_one.remote(41))
        await asyncio.sleep(0)
        assert not call_task.done()
        release_deploy.set()
        assert await call_task == 42

    asyncio.run(run())


def test_local_call_does_not_wait_for_background_deployment(monkeypatch) -> None:
    deploy_started = threading.Event()
    release_deploy = threading.Event()

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        deploy_started.set()
        assert release_deploy.wait(timeout=5)
        return self

    monkeypatch.setattr(RemoteFunction, "_deploy", fake_deploy)

    @function
    def add_one(value: int) -> int:
        return value + 1

    assert deploy_started.wait(timeout=5)
    assert add_one(41) == 42
    release_deploy.set()
    add_one._wait_for_background_deploy()


def test_background_deployment_error_is_raised_by_use(monkeypatch) -> None:
    error = FunctionBuildError("image failed")

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        raise error

    monkeypatch.setattr(RemoteFunction, "_deploy", fake_deploy)
    _install_fake_remote_call(monkeypatch, 42)

    @function
    def add_one(value: int) -> int:
        return value + 1

    with pytest.raises(FunctionBuildError) as exc_info:
        add_one.remote(41)

    assert exc_info.value is error


def test_with_knowledge_cutoff_clones_completed_deployment_state(monkeypatch) -> None:
    deploy_started = threading.Event()
    release_deploy = threading.Event()
    clone: list[RemoteFunction] = []

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        deploy_started.set()
        assert release_deploy.wait(timeout=5)
        self._function_id = "function-1"
        self._version_info = FunctionVersionInfo(
            id="version-1",
            function_id="function-1",
            function_name=self._func_name,
            version=1,
            scaling_group_name=self._func_name,
        )
        self._web_url = "https://function.example.com"
        return self

    monkeypatch.setattr(RemoteFunction, "_deploy", fake_deploy)

    @function
    def add_one(value: int) -> int:
        return value + 1

    assert deploy_started.wait(timeout=5)
    cloner = threading.Thread(
        target=lambda: clone.append(
            add_one.with_knowledge_cutoff("2026-01-01T00:00:00Z")
        )
    )
    cloner.start()
    release_deploy.set()
    cloner.join(timeout=5)

    assert not cloner.is_alive()
    assert clone[0].function_id == "function-1"
    assert clone[0].web_url == "https://function.example.com"


def test_explicit_deploy_after_decorator_creates_another_deployment(
    monkeypatch,
) -> None:
    deploy_count = 0
    deploy_lock = threading.Lock()

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        nonlocal deploy_count
        with deploy_lock:
            deploy_count += 1
        return self

    monkeypatch.setattr(RemoteFunction, "_deploy", fake_deploy)

    @function
    def add_one(value: int) -> int:
        return value + 1

    add_one._wait_for_background_deploy()
    add_one.deploy()

    assert deploy_count == 2


def test_definition_only_script_waits_for_all_deployments(tmp_path: Path) -> None:
    script = tmp_path / "deploy_functions.py"
    output_dir = tmp_path / "deployed"
    output_dir.mkdir()
    script.write_text(
        textwrap.dedent(
            """
            import os
            import threading
            from pathlib import Path

            from chalkcompute._function import RemoteFunction, function

            rendezvous = threading.Barrier(2)

            def fake_deploy(self, **kwargs):
                rendezvous.wait(timeout=5)
                (Path(os.environ["DEPLOY_OUTPUT_DIR"]) / self._func_name).write_text("done")
                return self

            RemoteFunction._deploy = fake_deploy

            @function
            def first(value: int) -> int:
                return value

            @function
            def second(value: int) -> int:
                return value
            """
        )
    )
    env = os.environ.copy()
    env["DEPLOY_OUTPUT_DIR"] = str(output_dir)
    repo_root = Path(__file__).parents[1]
    env["PYTHONPATH"] = os.pathsep.join([str(repo_root), env.get("PYTHONPATH", "")])

    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert {path.name for path in output_dir.iterdir()} == {"first", "second"}


def test_definition_only_script_reports_deployment_failure(tmp_path: Path) -> None:
    script = tmp_path / "deploy_failure.py"
    script.write_text(
        textwrap.dedent(
            """
            from chalkcompute._function import RemoteFunction, function

            def fake_deploy(self, **kwargs):
                raise RuntimeError("deployment exploded")

            RemoteFunction._deploy = fake_deploy

            @function
            def broken(value: int) -> int:
                return value
            """
        )
    )

    env = os.environ.copy()
    repo_root = Path(__file__).parents[1]
    env["PYTHONPATH"] = os.pathsep.join([str(repo_root), env.get("PYTHONPATH", "")])

    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert "Background deployment for function 'broken' failed" in completed.stderr
    assert "RuntimeError: deployment exploded" in completed.stderr
