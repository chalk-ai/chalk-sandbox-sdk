from __future__ import annotations

import inspect
import linecache
import textwrap
from pathlib import Path
from typing import Any

import pytest

from chalkcompute import traced
from chalkcompute._cls import RemoteClass, _NoOpClass, after, before, cls, method
from chalkcompute._cls import _strip_chalkcompute_from_class_source
from chalkcompute._function import (
    FunctionBuildError,
    RemoteFunction,
    _NoOpFunction,
    function,
)
from chalkcompute._function import (
    _REMOTE_ENV_VAR,
    _STRIP_SOURCE_ENV_VAR,
    _assert_stripped_source_self_contained,
    _source_file_for_deploy,
    _strip_chalkcompute_from_source,
)
from chalkcompute._remote_function_tracing import TracingPolicy


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


def test_function_decorator_passes_tracing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployed: list[RemoteFunction] = []

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        deployed.append(self)
        return self

    monkeypatch.setattr(RemoteFunction, "deploy", fake_deploy)

    @function(tracing=True)
    def traced_function() -> None:
        pass

    assert isinstance(traced_function, RemoteFunction)
    assert deployed[0]._tracing == TracingPolicy(sample_rate=1.0)


def test_function_deploy_image_installs_tracing_runtime_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_STRIP_SOURCE_ENV_VAR, "0")

    def add(x: int, y: int) -> int:
        return x + y

    fn = RemoteFunction(add)

    image = fn._build_deploy_image()

    assert image.to_proto().steps[0].pip_install.packages == [
        "chalk-remote-call-python[tracing]",
        "chalkcompute==0.0.0",
    ]


def test_function_deploy_image_installs_tracing_runtime_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_STRIP_SOURCE_ENV_VAR, "0")

    def add(x: int, y: int) -> int:
        return x + y

    fn = RemoteFunction(add, tracing=True)

    image = fn._build_deploy_image()

    assert image.to_proto().steps[0].pip_install.packages == [
        "chalk-remote-call-python[tracing]",
        "chalkcompute==0.0.0",
    ]


def test_function_deploy_image_installs_lightweight_runtime_when_tracing_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_STRIP_SOURCE_ENV_VAR, "0")

    def add(x: int, y: int) -> int:
        return x + y

    fn = RemoteFunction(add, tracing=False)

    image = fn._build_deploy_image()

    assert image.to_proto().steps[0].pip_install.packages == [
        "chalk-remote-call-python",
        "chalkcompute==0.0.0",
    ]


def test_stripped_function_deploy_image_installs_tracing_runtime_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_STRIP_SOURCE_ENV_VAR, "1")

    def add(x: int, y: int) -> int:
        return x + y

    fn = RemoteFunction(add, tracing=TracingPolicy(sample_rate=0.0))

    image = fn._build_deploy_image()

    assert image.to_proto().steps[0].pip_install.packages == [
        "chalk-remote-call-python[tracing]"
    ]


def test_function_uncalled_decorator_noops_in_remote_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_REMOTE_ENV_VAR, "1")

    @function
    def add(x: int, y: int) -> int:
        return x + y

    assert isinstance(add, _NoOpFunction)
    assert add(1, 2) == 3


def test_function_noop_preserves_function_kind_introspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_REMOTE_ENV_VAR, "1")

    @function
    def gen():
        yield "tick"

    @function
    async def agen():
        yield "tick"

    @function
    async def coro():
        return "tick"

    assert isinstance(gen, _NoOpFunction)
    assert inspect.isgeneratorfunction(gen)
    assert inspect.isgeneratorfunction(inspect.unwrap(gen))
    assert inspect.isasyncgenfunction(agen)
    assert inspect.isasyncgenfunction(inspect.unwrap(agen))
    assert inspect.iscoroutinefunction(coro)
    assert inspect.iscoroutinefunction(inspect.unwrap(coro))


def test_noop_function_remote_and_defer_resolve_deployed_sibling_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_REMOTE_ENV_VAR, "1")

    class _FakeBound:
        def __init__(self) -> None:
            self.remote_calls: list[tuple] = []
            self.defer_calls: list[tuple] = []
            self.cutoffs: list[Any] = []

        def remote(self, *a: Any, **kw: Any) -> str:
            self.remote_calls.append((a, kw))
            return "remote-result"

        def defer(self, *a: Any, **kw: Any) -> str:
            self.defer_calls.append((a, kw))
            return "handle"

        def with_knowledge_cutoff(self, cutoff: Any) -> "_FakeBound":
            self.cutoffs.append(cutoff)
            return self

    bound = _FakeBound()
    resolved: list[str] = []

    def fake_from_name(name: str, **_: Any) -> _FakeBound:
        resolved.append(name)
        return bound

    monkeypatch.setattr(RemoteFunction, "from_name", staticmethod(fake_from_name))

    @function(name="add-svc")
    def add(x: int, y: int) -> int:
        return x + y

    assert isinstance(add, _NoOpFunction)
    # __call__ runs the local body in-process, never resolving a sibling.
    assert add(1, 2) == 3
    assert resolved == []

    # .remote() / .defer() / .with_knowledge_cutoff() resolve the deployed
    # function lazily, using the *registered* name, and cache the handle.
    assert add.remote(1, 2) == "remote-result"
    assert add.defer(1, 2) == "handle"
    assert add.with_knowledge_cutoff("2026-01-01T00:00:00Z") is bound
    assert resolved == ["add-svc"]  # resolved once, then cached
    assert bound.remote_calls == [((1, 2), {})]
    assert bound.defer_calls == [((1, 2), {})]
    assert bound.cutoffs == ["2026-01-01T00:00:00Z"]


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


def test_noop_class_method_remote_and_defer_resolve_deployed_sibling_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_REMOTE_ENV_VAR, "1")

    class _FakeBound:
        def __init__(self) -> None:
            self.remote_calls: list[tuple] = []
            self.defer_calls: list[tuple] = []

        def remote(self, *a: Any, **kw: Any) -> str:
            self.remote_calls.append((a, kw))
            return "remote-result"

        def defer(self, *a: Any, **kw: Any) -> str:
            self.defer_calls.append((a, kw))
            return "handle"

    bound = _FakeBound()
    resolved: list[str] = []

    def fake_from_name(name: str, **_: Any) -> _FakeBound:
        resolved.append(name)
        return bound

    monkeypatch.setattr(RemoteFunction, "from_name", staticmethod(fake_from_name))

    @cls(name="adder-svc")
    class Adder:
        @method
        def add(self, x: int, y: int) -> int:
            return x + y

    assert isinstance(Adder, _NoOpClass)
    # __call__ runs the local method body in-process; no sibling resolved.
    assert Adder.add(1, 2) == 3
    assert resolved == []

    # .remote()/.defer() resolve the deployed method lazily, by "<class>.<method>",
    # using the registered class name. The proxy is cached on the class, so two
    # separate attribute accesses share one resolution.
    assert Adder.add.remote(1, 2) == "remote-result"
    assert Adder.add.defer(1, 2) == "handle"
    assert resolved == ["adder-svc.add"]
    assert bound.remote_calls == [((1, 2), {})]
    assert bound.defer_calls == [((1, 2), {})]


def test_remote_class_method_call_is_local_remote_is_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(RemoteClass, "deploy", lambda self, **_: self)

    @cls
    class Adder:
        @method
        def add(self, x: int, y: int) -> int:
            return x + y

    # Stand in a deployed RemoteFunction proxy for the 'add' method.
    class _FakeRF:
        def __init__(self) -> None:
            self.remote_calls: list[tuple] = []

        def remote(self, *a: Any, **kw: Any) -> int:
            self.remote_calls.append((a, kw))
            return 999

    fake_rf = _FakeRF()
    Adder._methods["add"].remote_function = fake_rf  # type: ignore[assignment]

    # __call__ runs the local body in-process (3), not the wire proxy (999).
    assert Adder.add(2, 1) == 3
    assert fake_rf.remote_calls == []

    # .remote() dispatches to the proxy.
    assert Adder.add.remote(2, 1) == 999
    assert fake_rf.remote_calls == [((2, 1), {})]


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


def test_function_source_strip_removes_direct_imported_traced_decorator() -> None:
    source = textwrap.dedent(
        """
        from chalkcompute import function as remote_function, traced as span

        @span("workflow")
        @remote_function
        def add(x: int, y: int) -> int:
            return x + y
        """
    )

    stripped = _strip_chalkcompute_from_source(source)

    assert "chalkcompute" not in stripped
    assert "@span" not in stripped
    assert "@remote_function" not in stripped
    namespace: dict[str, Any] = {}
    exec(stripped, namespace)
    assert namespace["add"](1, 2) == 3


def test_function_source_strip_removes_tracing_policy_decorator_arg() -> None:
    source = textwrap.dedent(
        """
        import chalkcompute

        @chalkcompute.function(
            tracing=chalkcompute.TracingPolicy(sample_rate=1.0),
        )
        def add(x: int, y: int) -> int:
            return x + y
        """
    )

    stripped = _strip_chalkcompute_from_source(source)

    assert "chalkcompute" not in stripped
    assert "TracingPolicy" not in stripped
    namespace: dict[str, Any] = {}
    exec(stripped, namespace)
    assert namespace["add"](1, 2) == 3


def test_source_file_for_deploy_unwraps_traced_function() -> None:
    @traced
    def add(x: int, y: int) -> int:
        return x + y

    assert Path(_source_file_for_deploy(add)).resolve() == Path(__file__).resolve()


def test_function_source_strip_handles_ipython_cached_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chalkcompute._function import _prepare_source_file

    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", "1")
    filename = "<ipython-input-3-53bd707a177b>"
    source = textwrap.dedent(
        """
        import chalkcompute

        @chalkcompute.function
        def test(x: str):
            print(x)
        """
    )
    linecache.cache[filename] = (
        len(source),
        None,
        source.splitlines(keepends=True),
        filename,
    )

    try:
        source_file, source_is_temp = _prepare_source_file(filename)
        try:
            stripped = Path(source_file).read_text()
        finally:
            Path(source_file).unlink()
    finally:
        linecache.cache.pop(filename, None)

    assert source_is_temp
    assert "chalkcompute" not in stripped
    assert "@chalkcompute.function" not in stripped
    assert "def test" in stripped


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


def test_function_rejects_none_env_value() -> None:
    """A ``None`` env value (e.g. os.getenv of an unset var) fails fast.

    Without this guard the ``None`` reaches the server as JSON ``null`` and is
    rejected with an opaque ``invalid value for string field value: null``.
    """
    with pytest.raises(TypeError) as exc_info:

        @function(env={"OPENAI_API_KEY": None})  # type: ignore[dict-item]
        def fn(x: int) -> int:
            return x

    message = str(exc_info.value)
    assert "OPENAI_API_KEY" in message
    assert "os.getenv" in message


def test_function_rejects_non_string_env_value() -> None:
    with pytest.raises(TypeError) as exc_info:

        @function(env={"PORT": 8080})  # type: ignore[dict-item]
        def fn(x: int) -> int:
            return x

    assert "PORT" in str(exc_info.value)


def test_function_accepts_string_env_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployed: list[RemoteFunction] = []

    def fake_deploy(self: RemoteFunction, **_: Any) -> RemoteFunction:
        deployed.append(self)
        return self

    monkeypatch.setattr(RemoteFunction, "deploy", fake_deploy)

    @function(env={"OPENAI_API_KEY": "sk-test"})
    def fn(x: int) -> int:
        return x

    assert deployed[0]._env["OPENAI_API_KEY"] == "sk-test"


# ---------------------------------------------------------------------------
# Strip-mode guard: nested chalkcompute references must survive stripping
# ---------------------------------------------------------------------------


def _strip_and_guard(source: str, entrypoint_sources: list[str] | None = None) -> None:
    """Strip a function source and run the self-contained guard on the result.

    ``fn`` is defined at module top level in these fixtures, so passing the whole
    source as the lone entrypoint lets the guard pick up ``fn``'s body.
    """
    stripped = _strip_chalkcompute_from_source(source)
    _assert_stripped_source_self_contained(
        source,
        stripped,
        label="Function 'fn'",
        entrypoint_sources=entrypoint_sources or [source],
    )


def test_strip_guard_rejects_dangling_module_reference() -> None:
    source = textwrap.dedent(
        """
        import chalkcompute

        @chalkcompute.function
        def fn() -> int:
            chalkcompute.gather([])
            return 1
        """
    )
    with pytest.raises(FunctionBuildError, match="chalkcompute"):
        _strip_and_guard(source)


def test_strip_guard_rejects_dangling_from_import_symbol() -> None:
    source = textwrap.dedent(
        """
        from chalkcompute import function, gather

        @function
        def fn() -> int:
            gather([])
            return 1
        """
    )
    with pytest.raises(FunctionBuildError, match="gather"):
        _strip_and_guard(source)


def test_strip_guard_allows_inner_import_that_survives_stripping() -> None:
    # The orchestrate pattern: a top-level import + decorator are stripped, but
    # the inner `import chalkcompute` survives and rebinds the name at runtime.
    source = textwrap.dedent(
        """
        import chalkcompute

        @chalkcompute.function(
            image=chalkcompute.Image.debian_slim().pip_install(["chalkcompute>=2.1.0"]),
        )
        def fn() -> int:
            import chalkcompute

            other = chalkcompute.RemoteFunction.from_name("other")
            return other.remote(1)
        """
    )
    # Must not raise — the inner import keeps the body self-contained.
    _strip_and_guard(source)


def test_strip_guard_allows_function_with_no_chalkcompute_usage() -> None:
    source = textwrap.dedent(
        """
        import chalkcompute

        @chalkcompute.function
        def fn(x: int) -> int:
            return x + 1
        """
    )
    _strip_and_guard(source)  # body references nothing from chalkcompute


def test_strip_guard_rejects_class_method_referencing_chalkcompute() -> None:
    # The cls strip removes *all* chalkcompute imports (including inner ones),
    # so any nested chalkcompute use in a method dangles after stripping.
    source = textwrap.dedent(
        """
        import chalkcompute

        @chalkcompute.cls
        class C:
            @chalkcompute.method
            def m(self) -> int:
                return chalkcompute.RemoteFunction.from_name("x").remote(1)
        """
    )
    stripped = _strip_chalkcompute_from_class_source(source)
    # The method body is the entrypoint; pass it dedented as its own snippet
    # (the cls strip removes even inner imports, so any chalkcompute use dangles).
    method_src = textwrap.dedent(
        """
        def m(self) -> int:
            return chalkcompute.RemoteFunction.from_name("x").remote(1)
        """
    )
    with pytest.raises(FunctionBuildError, match="chalkcompute"):
        _assert_stripped_source_self_contained(
            source, stripped, label="Class 'C'", entrypoint_sources=[method_src]
        )
