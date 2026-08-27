from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from chalkcompute._function import (
    FunctionBuildError,
    FunctionError,
    FunctionVersionInfo,
    RemoteFunction,
    function,
)
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb


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
            from concurrent.futures import ThreadPoolExecutor
            from pathlib import Path

            from chalkcompute._function import RemoteFunction, function

            rendezvous = threading.Barrier(2)

            def fake_deploy(self, **kwargs):
                rendezvous.wait(timeout=5)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    result = executor.submit(lambda: "done").result()
                (Path(os.environ["DEPLOY_OUTPUT_DIR"]) / self._func_name).write_text(result)
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

    # A definition-only script must fail, not exit 0 on code that cannot serve.
    assert completed.returncode != 0
    assert "Background deployment for function 'broken' failed" in completed.stderr
    assert "RuntimeError: deployment exploded" in completed.stderr


def _version_info(name: str) -> FunctionVersionInfo:
    return FunctionVersionInfo(
        id="version-1",
        function_id="function-1",
        function_name=name,
        version=1,
        scaling_group_name=f"sg-{name}",
    )


def test_wait_ready_polls_readiness_after_the_background_deploy(monkeypatch) -> None:
    """The background deploy is a precondition, not a substitute for polling."""
    polled: list[str] = []

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        self._function_id = "function-1"
        self._version_info = _version_info(self._func_name)
        return self

    class _FakeScalingGroup:
        def __init__(self, ready: bool) -> None:
            self.status = "Available" if ready else "Pending"
            self.web_url = "https://function.example.com"
            self.ready_replicas = 1 if ready else 0
            self.available_replicas = 1 if ready else 0

    def fake_get_scaling_group(_client: Any, name: str) -> Any:
        polled.append(name)
        return _FakeScalingGroup(ready=len(polled) >= 2)

    monkeypatch.setattr(RemoteFunction, "_deploy", fake_deploy)
    monkeypatch.setattr(
        "chalkcompute._function._get_scaling_group", fake_get_scaling_group
    )
    monkeypatch.setattr(
        "chalkcompute._function._scaling_group_is_ready",
        lambda sg, **_: sg.status == "Available",
    )

    @function
    def add_one(value: int) -> int:
        return value + 1

    add_one.wait_ready(timeout=5.0, poll_interval=0.0)

    # Two polls: the first saw Pending, so the loop was genuinely entered.
    assert polled == ["sg-add_one", "sg-add_one"]


def test_wait_ready_timeout_covers_readiness_not_just_the_deploy(monkeypatch) -> None:
    """A handle that deploys but never becomes ready must raise, not succeed."""

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        self._function_id = "function-1"
        self._version_info = _version_info(self._func_name)
        return self

    class _NeverReady:
        status = "Pending"
        web_url = None
        ready_replicas = 0
        available_replicas = 0

    monkeypatch.setattr(RemoteFunction, "_deploy", fake_deploy)
    monkeypatch.setattr(
        "chalkcompute._function._get_scaling_group",
        lambda _client, _name: _NeverReady(),
    )
    monkeypatch.setattr(
        "chalkcompute._function._scaling_group_is_ready", lambda sg, **_: False
    )

    @function
    def add_one(value: int) -> int:
        return value + 1

    with pytest.raises(FunctionError, match="not ready after"):
        add_one.wait_ready(timeout=0.15, poll_interval=0.01)


def test_explicit_deploy_waits_for_the_decorators_deployment(monkeypatch) -> None:
    """``.deploy()`` must not race the decorator's deploy for the same name.

    Fakes ``_deploy`` and drives the real public ``deploy`` wrapper, so the
    barrier itself is exercised rather than replaced.
    """
    calls: list[int] = []
    calls_lock = threading.Lock()
    background_running = threading.Event()
    release_background = threading.Event()

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        with calls_lock:
            index = len(calls)
            calls.append(index)
        if index == 0:
            background_running.set()
            assert release_background.wait(timeout=5)
        return self

    monkeypatch.setattr(RemoteFunction, "_deploy", fake_deploy)

    @function
    def add_one(value: int) -> int:
        return value + 1

    assert background_running.wait(timeout=5)

    manual = threading.Thread(target=add_one.deploy)
    manual.start()
    try:
        # The manual deploy is parked on the barrier, so it has not entered
        # _deploy while the decorator's deployment is still running.
        time.sleep(0.05)
        with calls_lock:
            assert calls == [0], calls
    finally:
        release_background.set()
        manual.join(timeout=5)

    assert not manual.is_alive()
    with calls_lock:
        assert calls == [0, 1]


def test_strip_mode_violation_fails_the_defining_script(tmp_path: Path) -> None:
    """A strip-mode violation must fail the process, not exit 0 at teardown.

    The guard is local and needs no network, so it runs at decoration time; a
    definition-only deploy script therefore fails loudly instead of reporting
    the error from an atexit hook after the process has already succeeded.
    """
    script = tmp_path / "strip_violation.py"
    script.write_text(
        textwrap.dedent(
            """
            import chalkcompute


            @chalkcompute.function
            def broken(value: int) -> int:
                return len(chalkcompute.__name__) + value
            """
        )
    )

    env = os.environ.copy()
    repo_root = Path(__file__).parents[1]
    env["PYTHONPATH"] = os.pathsep.join([str(repo_root), env.get("PYTHONPATH", "")])
    env["CHALK_FUNCTION_STRIP_SOURCE"] = "1"

    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert "references chalkcompute" in completed.stderr
    # Raised inline, not deferred to the background-deployment reporter.


def test_wait_ready_polls_once_when_the_deploy_used_the_whole_budget(
    monkeypatch,
) -> None:
    """Exhausting the budget in the deploy must not skip the readiness check.

    ``wait_ready`` shares one deadline across the deployment wait and the
    readiness poll. A deployment that finishes at the very end of the budget
    must still get one poll, otherwise a function that is already serving is
    reported as never ready.
    """
    polled: list[str] = []

    def fake_wait(self: RemoteFunction, timeout: float | None = None) -> bool:
        self._function_id = "function-1"
        self._version_info = _version_info(self._func_name)
        if timeout:
            # Deployment succeeds, but consumes the caller's whole budget.
            time.sleep(timeout)
        return True

    class _Ready:
        status = "Available"
        web_url = "https://function.example.com"
        ready_replicas = 1
        available_replicas = 1

    def fake_get_scaling_group(_client: Any, name: str) -> Any:
        polled.append(name)
        return _Ready()

    monkeypatch.setattr(RemoteFunction, "_wait_for_background_deploy", fake_wait)
    monkeypatch.setattr(
        "chalkcompute._function._get_scaling_group", fake_get_scaling_group
    )
    monkeypatch.setattr(
        "chalkcompute._function._scaling_group_is_ready", lambda sg, **_: True
    )

    def add_one(value: int) -> int:
        return value + 1

    RemoteFunction(add_one).wait_ready(timeout=0.05, poll_interval=0.0)

    assert polled == ["sg-add_one"]


def test_explicit_deploy_retries_after_a_failed_background_deployment(
    monkeypatch,
) -> None:
    """A failed decorator deployment must not poison later explicit deploys."""
    attempts: list[int] = []
    attempts_lock = threading.Lock()

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        with attempts_lock:
            attempt = len(attempts)
            attempts.append(attempt)
        if attempt == 0:
            raise FunctionBuildError("transient image failure")
        self._function_id = "function-1"
        self._version_info = _version_info(self._func_name)
        return self

    monkeypatch.setattr(RemoteFunction, "_deploy", fake_deploy)

    @function
    def add_one(value: int) -> int:
        return value + 1

    # The background attempt failed; an explicit deploy is a fresh attempt.
    add_one.deploy()

    with attempts_lock:
        assert attempts == [0, 1]
    # The handle is no longer bound to the failed attempt, so ordinary use works.
    assert add_one.function_id == "function-1"


def test_failed_background_deployment_still_raises_on_use(monkeypatch) -> None:
    """Retry is scoped to deploy(); other call sites keep the real error."""
    error = FunctionBuildError("image failed")

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        raise error

    monkeypatch.setattr(RemoteFunction, "_deploy", fake_deploy)

    @function
    def add_one(value: int) -> int:
        return value + 1

    with pytest.raises(FunctionBuildError) as exc_info:
        add_one.version_info

    assert exc_info.value is error


def test_wait_ready_accepts_scaled_to_zero_for_scale_to_zero_functions(
    monkeypatch,
) -> None:
    """``min_replicas=0`` is ready at ScaledToZero, as ``deploy`` already treats it.

    Uses a real ScalingGroupResponse so the actual readiness predicate runs.
    """
    polled: list[str] = []

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        self._function_id = "function-1"
        self._version_info = _version_info(self._func_name)
        return self

    def fake_get_scaling_group(_client: Any, name: str) -> Any:
        polled.append(name)
        return sg_pb.ScalingGroupResponse(
            id="sg-1",
            name=name,
            status="ScaledToZero",
            ready_replicas=0,
            available_replicas=0,
        )

    monkeypatch.setattr(RemoteFunction, "_deploy", fake_deploy)
    monkeypatch.setattr(
        "chalkcompute._function._get_scaling_group", fake_get_scaling_group
    )

    @function(min_replicas=0, max_replicas=2)
    def idle(value: int) -> int:
        return value

    # Would otherwise burn the whole timeout and raise "not ready".
    idle.wait_ready(timeout=0.2, poll_interval=0.01)

    assert polled == ["sg-idle"]


def _scaling_group(name: str, *, status: str, min_replicas: int) -> Any:
    return sg_pb.ScalingGroupResponse(
        id="sg-1",
        name=name,
        status=status,
        ready_replicas=0,
        available_replicas=0,
        spec=sg_pb.ScalingSpec(min_replicas=min_replicas, max_replicas=2),
    )


def test_wait_ready_trusts_the_servers_min_replicas_over_the_local_default(
    monkeypatch,
) -> None:
    """A from_name handle never learns min_replicas, so read it off the group."""
    polled: list[str] = []

    def fake_get_scaling_group(_client: Any, name: str) -> Any:
        polled.append(name)
        return _scaling_group(name, status="ScaledToZero", min_replicas=0)

    monkeypatch.setattr(
        "chalkcompute._function._get_scaling_group", fake_get_scaling_group
    )

    def idle(value: int) -> int:
        return value

    # _make_empty-style handles hardcode _min_replicas = 1, which would
    # otherwise demand a replica a scale-to-zero function will never have.
    fn = RemoteFunction(idle)
    assert fn._min_replicas == 1
    fn._version_info = _version_info("idle")

    fn.wait_ready(timeout=0.2, poll_interval=0.01)

    assert polled == ["sg-idle"]


def test_wait_ready_still_demands_a_replica_when_the_server_says_so(
    monkeypatch,
) -> None:
    """The server's spec must not be read as "scale to zero" when it isn't."""

    def fake_get_scaling_group(_client: Any, name: str) -> Any:
        return _scaling_group(name, status="Running", min_replicas=1)

    monkeypatch.setattr(
        "chalkcompute._function._get_scaling_group", fake_get_scaling_group
    )

    def busy(value: int) -> int:
        return value

    fn = RemoteFunction(busy)
    fn._version_info = _version_info("busy")

    with pytest.raises(FunctionError, match="not ready after"):
        fn.wait_ready(timeout=0.05, poll_interval=0.01)


def test_a_new_decorator_queues_behind_an_in_flight_manual_deploy(
    monkeypatch,
) -> None:
    """A manual deploy must be visible to the chain for its registered name."""
    order: list[str] = []
    order_lock = threading.Lock()
    manual_running = threading.Event()
    release_manual = threading.Event()

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        with order_lock:
            first = not order
        if first:
            order.append("manual:start")
            manual_running.set()
            assert release_manual.wait(timeout=5)
            with order_lock:
                order.append("manual:end")
        else:
            with order_lock:
                order.append("decorator")
        return self

    monkeypatch.setattr(RemoteFunction, "_deploy", fake_deploy)

    def existing(value: int) -> int:
        return value

    handle = RemoteFunction(existing, name="shared-name")
    manual = threading.Thread(target=handle.deploy)
    manual.start()
    try:
        assert manual_running.wait(timeout=5)

        # Decorating the same registered name now must queue, not race.
        @function(name="shared-name")
        def replacement(value: int) -> int:
            return value

        time.sleep(0.05)
        with order_lock:
            assert order == ["manual:start"], order
    finally:
        release_manual.set()
        manual.join(timeout=5)

    replacement._wait_for_background_deploy()
    assert order == ["manual:start", "manual:end", "decorator"]
