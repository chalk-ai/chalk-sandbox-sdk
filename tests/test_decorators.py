from __future__ import annotations

import textwrap
from typing import Any

import pytest

from chalkcompute._cls import RemoteClass, _NoOpClass, after, before, cls, method
from chalkcompute._cls import _strip_chalkcompute_from_class_source
from chalkcompute._function import RemoteFunction, _NoOpFunction, function
from chalkcompute._function import _REMOTE_ENV_VAR, _strip_chalkcompute_from_source


def test_function_decorator_accepts_called_and_uncalled_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployed: list[RemoteFunction] = []

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        deployed.append(self)
        return self

    monkeypatch.setattr(RemoteFunction, "deploy", fake_deploy)

    @function
    def bare(x: int) -> int:
        return x

    @function()
    def called(x: int) -> int:
        return x

    assert isinstance(bare, RemoteFunction)
    assert isinstance(called, RemoteFunction)
    assert [deployed_fn._func_name for deployed_fn in deployed] == ["bare", "called"]


def test_function_decorator_passes_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployed: list[RemoteFunction] = []

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        deployed.append(self)
        return self

    monkeypatch.setattr(RemoteFunction, "deploy", fake_deploy)

    @function(schedule=" 1h ")
    def scheduled() -> None:
        pass

    assert isinstance(scheduled, RemoteFunction)
    assert deployed[0]._schedule == "1h"


def test_function_uncalled_decorator_noops_in_remote_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_REMOTE_ENV_VAR, "1")

    @function
    def add(x: int, y: int) -> int:
        return x + y

    assert isinstance(add, _NoOpFunction)
    assert add(1, 2) == 3


def test_class_decorators_accept_called_and_uncalled_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployed: list[RemoteClass] = []

    def fake_deploy(self: RemoteClass, **_: Any) -> RemoteClass:
        deployed.append(self)
        return self

    monkeypatch.setattr(RemoteClass, "deploy", fake_deploy)

    @cls
    class BareAdder:
        @before
        def startup(self) -> None:
            pass

        @method
        def add(self, x: int, y: int) -> int:
            return x + y

        @after
        def shutdown(self) -> None:
            pass

    @cls()
    class CalledAdder:
        @before()
        def startup(self) -> None:
            pass

        @method()
        def add(self, x: int, y: int) -> int:
            return x + y

        @after()
        def shutdown(self) -> None:
            pass

    assert isinstance(BareAdder, RemoteClass)
    assert isinstance(CalledAdder, RemoteClass)
    assert [deployed_class.class_name for deployed_class in deployed] == [
        "BareAdder",
        "CalledAdder",
    ]
    assert set(BareAdder._methods) == {"add"}
    assert BareAdder._before_name == "startup"
    assert BareAdder._after_name == "shutdown"


def test_cls_decorator_passes_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployed: list[RemoteClass] = []

    def fake_deploy(self: RemoteClass, **_: Any) -> RemoteClass:
        deployed.append(self)
        return self

    monkeypatch.setattr(RemoteClass, "deploy", fake_deploy)

    @cls(schedule="0 * * * *")
    class ScheduledJobs:
        @method
        def tick(self) -> None:
            pass

    assert isinstance(ScheduledJobs, RemoteClass)
    assert deployed[0]._schedule == "0 * * * *"


def test_cls_uncalled_decorator_noops_in_remote_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_REMOTE_ENV_VAR, "1")

    @cls
    class Adder:
        @method
        def add(self, x: int, y: int) -> int:
            return x + y

    assert isinstance(Adder, _NoOpClass)
    assert Adder.add(1, 2) == 3


def test_function_source_strip_handles_uncalled_direct_import() -> None:
    source = textwrap.dedent(
        """
        from chalkcompute import function

        @function
        def add(x: int, y: int) -> int:
            return x + y

        add.wait_ready()
        """
    )

    stripped = _strip_chalkcompute_from_source(source)

    assert "chalkcompute" not in stripped
    assert "@function" not in stripped
    assert "wait_ready" not in stripped
    namespace: dict[str, Any] = {}
    exec(stripped, namespace)
    assert namespace["add"](1, 2) == 3


def test_class_source_strip_handles_uncalled_direct_imports() -> None:
    source = textwrap.dedent(
        """
        from chalkcompute import after, before, cls, method

        @cls
        class Adder:
            @before
            def startup(self) -> None:
                self.ready = True

            @method
            def add(self, x: int, y: int) -> int:
                return x + y

            @after
            def shutdown(self) -> None:
                self.ready = False

        Adder.wait_ready()
        """
    )

    stripped = _strip_chalkcompute_from_class_source(source)

    assert "chalkcompute" not in stripped
    assert "@cls" not in stripped
    assert "@method" not in stripped
    assert "@before" not in stripped
    assert "@after" not in stripped
    assert "wait_ready" not in stripped
    namespace: dict[str, Any] = {}
    exec(stripped, namespace)
    assert namespace["Adder"]().add(1, 2) == 3
