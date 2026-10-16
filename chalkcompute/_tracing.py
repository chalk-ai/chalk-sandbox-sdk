"""OpenTelemetry tracing for chalkcompute.

Span export is configured when OTEL_EXPORTER_OTLP_ENDPOINT or
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT is set in the environment. When neither is
present and no application tracer provider is installed, tracing operations use
OpenTelemetry's no-op provider.
"""

from __future__ import annotations

import atexit
import contextlib
import functools
import importlib
import inspect
import os
import weakref
from collections.abc import Callable, Generator
from typing import Any, NamedTuple, TypeVar, cast, overload

F = TypeVar("F", bound=Callable[..., Any])

_TRACER_NAME = "chalkcompute"
_DEFAULT_PUBLIC_SPAN_NAME = "chalkcompute.span"
_TRACE_CONTEXT_METADATA_KEYS = ("traceparent", "tracestate", "baggage")
_TRACE_CONTEXT_ENV_VARS = tuple(key.upper() for key in _TRACE_CONTEXT_METADATA_KEYS)
_REMOTE_FUNCTION_TRACE_POLICY_ENV_VAR = "CHALK_REMOTE_FUNCTION_TRACE_POLICY"
_OTEL_TRACES_SAMPLER_ENV_VAR = "OTEL_TRACES_SAMPLER"

SESSION_ID_ATTRIBUTE = "session.id"
"""Span attribute Chalk resolves an agent session from, first of six spellings.

Kept in sync with ``SESSION_ID_ATTRIBUTE_KEYS`` in chalk-private's
``engine/chalkengine/chalksql/providers/trace_query.py`` and the ClickHouse
``SessionId`` column expression.
"""

SESSION_ID_HEADER = "x-chalk-session-id"
"""Request header, lowercased, that places a call's spans in a session.

Chalk sends it from ``catalog_call('fn', ..., metadata => {'x-chalk-session-id':
s})``; :func:`chalkcompute.get_call_context` exposes it, and
:func:`enter_session` stamps :data:`SESSION_ID_ATTRIBUTE` from it.

This module owns these constants because it is a leaf — ``_trace`` re-exports
them for the public API and ``_call_context`` reads the header and row-metadata
keys, so the spellings cannot drift between the three.
"""

ENVIRONMENT_ID_ATTRIBUTE = "chalk.environment_id"
"""Attribute naming the Chalk environment a span belongs to.

Chalk's ClickHouse `EnvironmentId` column reads this off the span's *resource*, which the
remote-call runtime sets from :data:`ENVIRONMENT_ID_ENV_VAR` — correct for this process,
which serves exactly one environment. It is additionally sent as baggage on outbound calls
so a *shared* service, which serves many environments and therefore cannot put it on a
process-wide resource, can attribute the call it is serving.

Interim: baggage makes this caller-asserted, so it is an attribution hint, not a tenancy
boundary. META-533 tracks moving the shared-service side to the request's JWT principal.
"""

ENVIRONMENT_ID_ENV_VAR = "CHALK_ENVIRONMENT_ID"
"""Environment variable every Chalk workload pod carries its environment id in."""

SESSION_ID_ROW_METADATA_KEY = "__chalk__session_id__"
"""Key under which a row's metadata blob may name the session that row belongs to.

Read when the blob parses as a UTF-8 JSON object, from
``catalog_call('fn', ..., row_metadata => <binary expr>)``. Per row, unlike
:data:`SESSION_ID_HEADER`, which is per request — so this is what a batch whose
rows belong to different conversations has to use.
"""

SESSION_ROOT_SPAN_NAME = "chalkcompute.session"
"""Name of the root span :func:`enter_session` opens for a session's own trace."""

_initialized = False
_provider: Any | None = None
_user_tracing_configured = False
_ambient_context_adopted = False
_tracer: Any | None = None
_session_processor_providers: weakref.WeakSet[Any] = weakref.WeakSet()


def _is_enabled() -> bool:
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    )


def _validate_sample_rate(sample_rate: float) -> float:
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int | float):
        raise TypeError("sample_rate must be a number between 0.0 and 1.0")
    rate = float(sample_rate)
    if rate < 0.0 or rate > 1.0:
        raise ValueError("sample_rate must be between 0.0 and 1.0")
    return rate


@functools.cache
def _get_otel_module(module_name: str) -> Any | None:
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


def _get_context_api() -> Any | None:
    return _get_otel_module("opentelemetry.context")


def _get_propagate_api() -> Any | None:
    return _get_otel_module("opentelemetry.propagate")


def _get_trace_api() -> Any | None:
    return _get_otel_module("opentelemetry.trace")


def _require_trace_api() -> Any:
    trace_api = _get_trace_api()
    if trace_api is None:
        raise ImportError("opentelemetry.trace is required for tracing")
    return trace_api


def _has_tracing_intent() -> bool:
    # Internal SDK instrumentation should stay cold until Chalk tracing is
    # configured or OTEL exporter env vars indicate process-level tracing.
    return _provider is not None or _user_tracing_configured or _is_enabled()


def _session_processor_class() -> Any:
    """Build the session-stamping processor class, lazily.

    Defined inside a function because it subclasses an optional dependency.
    """
    import opentelemetry.baggage as baggage_api
    from opentelemetry.sdk.trace import SpanProcessor

    class _SessionIdSpanProcessor(SpanProcessor):
        """Copy the active session id onto every span started in its context.

        Chalk resolves an agent session from the ``session.id`` *span*
        attribute, so the id has to be on the spans before export — including
        spans this SDK never creates, such as those from LLM instrumentation.
        A processor on the tracer provider is the only hook that sees every
        span's start regardless of which tracer made it.

        A resource attribute would not do: the resource is built once per
        provider and is process-wide, while one function pod serves rows from
        many different sessions.
        """

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            # on_start is handed the parent context explicitly; it is not
            # necessarily the current one. This runs for every span in the
            # process, so it stays a single baggage lookup.
            session_id = baggage_api.get_baggage(
                SESSION_ID_ATTRIBUTE, context=parent_context
            )
            if session_id:
                span.set_attribute(SESSION_ID_ATTRIBUTE, str(session_id))

    return _SessionIdSpanProcessor


def _install_session_processor(provider: Any) -> bool:
    """Attach the session-stamping processor to ``provider``. Idempotent per provider.

    Tracked per provider rather than once per process. The provider chalkcompute builds is
    not always the one that ends up installed — in a remote function pod the remote-call
    runtime installs its own first, so ``set_tracer_provider`` is refused and ours is left
    orphaned — and a single process-wide flag let that orphan's install suppress the live
    provider's, so no exported span was ever stamped.
    """
    add_span_processor = getattr(provider, "add_span_processor", None)
    if add_span_processor is None:
        # The API's no-op provider creates no spans to stamp.
        return False
    if provider in _session_processor_providers:
        return True
    try:
        add_span_processor(_session_processor_class()())
    except ImportError:
        return False
    try:
        _session_processor_providers.add(provider)
    except TypeError:
        # Not weak-referenceable, so this install cannot be deduped. Every SDK provider is;
        # only an exotic fake gets here, and it pays with a duplicate (idempotent) stamp.
        pass
    return True


class _SessionScope(NamedTuple):
    """What :func:`enter_session` attached, and the span it opened, if any."""

    context: object
    span: Any | None


def _open_session_trace(
    trace_api: Any, context: Any, session_id: str
) -> tuple[Any, Any]:
    """Open the session's own trace, linked back to the span that encloses it.

    A trace carries at most one session: Chalk resolves the session of a whole trace as a
    ``max()`` over its spans' ``session.id``. So a session must not share a trace with a
    different one — and one remote call serves a whole batch of rows under a single
    invocation span, with each row free to name its own session. Joining that span would
    collapse every session in the batch into whichever id sorted highest, and would sweep
    the session-less rows in with them.

    Severing instead gives each session its own trace, and recording the enclosing span as
    a link keeps the call and its rows navigable from either end. Owning a root span (rather
    than only cutting the context) also means the row's spans stay in one trace instead of
    each sibling starting its own, and that a row emitting no spans of its own still shows
    up in the session.

    Returns ``(span, context)`` — the opened span, and a context with it current.
    """
    enclosing = trace_api.get_current_span(context).get_span_context()
    links = []
    if enclosing.is_valid:
        links.append(trace_api.Link(enclosing, {"chalk.link.kind": "enclosing_call"}))
        # A span only starts a new trace when its context names no valid parent.
        context = trace_api.set_span_in_context(trace_api.INVALID_SPAN, context)
    span = get_tracer().start_span(SESSION_ROOT_SPAN_NAME, context=context, links=links)
    span.set_attribute(SESSION_ID_ATTRIBUTE, session_id)
    return span, trace_api.set_span_in_context(span, context)


def enter_session(session_id: str) -> object | None:
    """Place ``session_id`` in the current OpenTelemetry context.

    Every span started until :func:`exit_session` is stamped with
    ``session.id``, and the id rides outbound calls as W3C ``baggage`` — so a
    nested compute function's spans join the same session without extra
    plumbing.

    Unless the context is already in this same session, the spans are placed in a **trace of
    their own**, rooted at a :data:`SESSION_ROOT_SPAN_NAME` span linked to the enclosing one.
    A trace belongs to one session, and the rows of a single remote call — which share an
    invocation span, and therefore a trace — may each name a different one. Re-entering the
    session already in scope joins it, so a nested call does not split the conversation.

    Returns
    -------
    object or None
        A token for :func:`exit_session`, or ``None`` when OpenTelemetry is
        unavailable or tracing is off.
    """
    if _is_remote_tracing_off() or not session_id:
        return None

    otel_context = _get_context_api()
    baggage_api = _get_otel_module("opentelemetry.baggage")
    if otel_context is None or baggage_api is None:
        return None

    _ensure_initialized()
    trace_api = _get_trace_api()
    if trace_api is not None:
        _install_session_processor(trace_api.get_tracer_provider())

    already_in_session = current_session_id() == session_id
    context = baggage_api.set_baggage(SESSION_ID_ATTRIBUTE, session_id)
    span = None
    if trace_api is not None and not already_in_session:
        span, context = _open_session_trace(trace_api, context, session_id)
    return _SessionScope(otel_context.attach(context), span)


def exit_session(token: object) -> None:
    """Undo :func:`enter_session`."""
    context, span = (
        (token.context, token.span)
        if isinstance(token, _SessionScope)
        else (token, None)
    )
    otel_context = _get_context_api()
    if otel_context is not None:
        otel_context.detach(context)
    if span is not None:
        span.end()


def current_session_id() -> str | None:
    """Return the session id spans in this context are being stamped with."""
    baggage_api = _get_otel_module("opentelemetry.baggage")
    if baggage_api is None:
        return None
    value = baggage_api.get_baggage(SESSION_ID_ATTRIBUTE)
    return None if value is None else str(value)


def _installed_provider(trace_api: Any) -> Any | None:
    """The process-wide tracer provider, when a real one is already installed.

    Until something calls ``set_tracer_provider`` the API hands back a proxy that takes no
    span processor, so "a real one is installed" is exactly "the current provider accepts
    span processors".
    """
    provider = trace_api.get_tracer_provider()
    return provider if hasattr(provider, "add_span_processor") else None


def _default_sample_rate() -> float | None:
    """The root sample rate when the caller names none.

    ``None`` defers to ``OTEL_TRACES_SAMPLER`` / ``OTEL_TRACES_SAMPLER_ARG``
    when the environment sets them — inside a Chalk workload the platform (or
    ``Sandbox(tracing=...)``) does — so ``chalkcompute`` samples exactly as
    every other OpenTelemetry SDK in the container does. Otherwise 1.0.
    """
    return None if os.environ.get(_OTEL_TRACES_SAMPLER_ENV_VAR) else 1.0


def _install_tracer_provider(sample_rate: float | None) -> None:
    global _provider, _tracer
    if _provider is not None:
        return

    trace_api = _require_trace_api()

    # Adopt a provider someone else already installed rather than building a second one.
    # In a remote function pod the chalk-remote-call runtime installs one before user code
    # runs; ``set_tracer_provider`` would only log "Overriding of current TracerProvider is
    # not allowed" and keep theirs, leaving ours holding an exporter that never sees a span
    # — because ``get_tracer`` resolves through the global provider either way. ``sample_rate``
    # belongs to the provider that owns the process, so it does not apply to an adopted one.
    installed = _installed_provider(trace_api)
    if installed is not None:
        _install_session_processor(installed)
        _provider = installed
        _tracer = None
        return

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    resource_attributes = {"service.name": "chalkcompute"}
    if environment_id := os.environ.get("CHALK_ENVIRONMENT_ID"):
        resource_attributes["chalk.environment_id"] = environment_id
    resource = Resource.create(resource_attributes)
    # ``sampler=None`` makes the SDK read the OTEL_TRACES_SAMPLER environment;
    # see ``_default_sample_rate``.
    sampler = (
        None if sample_rate is None else ParentBased(TraceIdRatioBased(sample_rate))
    )
    provider = TracerProvider(resource=resource, sampler=sampler)

    if _is_enabled():
        from chalkcompute._telemetry_export import create_span_exporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider.add_span_processor(BatchSpanProcessor(create_span_exporter()))

    # Before set_tracer_provider, so no span can start unstamped.
    _install_session_processor(provider)

    trace_api.set_tracer_provider(provider)
    _provider = provider
    _tracer = None
    atexit.register(provider.shutdown)


def _configure_tracing(*, sample_rate: float | None = 1.0) -> None:
    """Configure caller-side tracing for this process.

    The sampler is parent-based: existing parent trace decisions are respected,
    and ``sample_rate`` is only used when this process starts a new root trace.
    ``None`` defers the root rate to the ``OTEL_TRACES_SAMPLER`` environment.
    """
    global _initialized, _user_tracing_configured
    rate = None if sample_rate is None else _validate_sample_rate(sample_rate)
    _install_tracer_provider(rate)
    _initialized = True
    _user_tracing_configured = True
    _adopt_ambient_trace_context()


def _adopt_ambient_trace_context() -> object | None:
    """Parent this process's spans to ``TRACEPARENT``/``TRACESTATE``/``BAGGAGE``.

    :meth:`Sandbox.exec` and :meth:`Sandbox.execute_python` hand the caller's
    trace context to the process they start as these environment variables —
    the W3C header names, spelled as env vars the way otel-cli and shell
    integrations do — so a script inside a sandbox joins the caller's trace
    without any code of its own. Attached once, to the thread that initializes
    tracing, and never over a parent that is already current in-process.

    Returns the attach token when a context was adopted, else ``None``.
    """
    global _ambient_context_adopted
    if _ambient_context_adopted:
        return None
    _ambient_context_adopted = True

    carrier = {
        key.lower(): value
        for key in _TRACE_CONTEXT_ENV_VARS
        if (value := os.environ.get(key))
    }
    if "traceparent" not in carrier:
        return None
    otel_context = _get_context_api()
    propagate_api = _get_propagate_api()
    trace_api = _get_trace_api()
    if otel_context is None or propagate_api is None or trace_api is None:
        return None
    if trace_api.get_current_span().get_span_context().is_valid:
        return None
    context = propagate_api.extract(carrier)
    if not trace_api.get_current_span(context).get_span_context().is_valid:
        return None
    return otel_context.attach(context)


def _ensure_initialized() -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    if _is_enabled():
        _install_tracer_provider(_default_sample_rate())
    # Even without an exporter, joining the ambient parent lets
    # ``current_trace_metadata`` carry it on to nested calls.
    _adopt_ambient_trace_context()


def _ensure_user_tracing_configured() -> None:
    if not _user_tracing_configured:
        _configure_tracing(sample_rate=_default_sample_rate())


def _request_tracing_intent() -> None:
    """Open the SDK's own spans because the caller asked for a traced workload.

    ``Sandbox(tracing=True)`` calls this: the workload can only join the
    caller's trace if the caller has one to hand over, and internal
    ``_instrumented`` spans stay cold until something signals intent.
    """
    if _is_remote_tracing_off():
        return
    _ensure_user_tracing_configured()


def _is_remote_tracing_off() -> bool:
    return os.environ.get(_REMOTE_FUNCTION_TRACE_POLICY_ENV_VAR) == "always_off"


def get_tracer() -> Any:
    """Return a tracer for ``chalkcompute``.

    Returns a configured tracer if OTLP env vars are set, otherwise the
    default no-op tracer.

    Returns
    -------
    opentelemetry.trace.Tracer
        The configured or no-op tracer.
    """
    global _tracer
    _ensure_initialized()
    if _tracer is None:
        _tracer = _require_trace_api().get_tracer(_TRACER_NAME)
    return _tracer


def has_current_trace_context() -> bool:
    """Return whether the current context carries a valid trace."""
    trace_api = _get_trace_api()
    if trace_api is None:
        return False

    if not _is_remote_tracing_off():
        _ensure_initialized()
    span_context = trace_api.get_current_span().get_span_context()
    return bool(span_context.is_valid)


def _outbound_context() -> Any | None:
    """The current context, plus this process's environment id in baggage.

    Added at injection rather than attached to the local context: the environment is a
    property of this process, so locally-started spans already carry it on their resource,
    and putting it in the ambient context would only duplicate it onto every one of them.
    On the wire it is the only way a shared service — one deployment serving many
    environments, so its resource cannot name any of them — can attribute the call.

    Returns ``None`` when there is no environment id or no baggage API, which
    ``inject`` reads as "use the current context".
    """
    environment_id = os.environ.get(ENVIRONMENT_ID_ENV_VAR)
    if not environment_id:
        return None
    baggage_api = _get_otel_module("opentelemetry.baggage")
    if baggage_api is None:
        return None
    return baggage_api.set_baggage(ENVIRONMENT_ID_ATTRIBUTE, environment_id)


def current_trace_metadata() -> list[tuple[str, str]]:
    """Return W3C trace metadata for the current OpenTelemetry context."""
    if not has_current_trace_context():
        return []

    propagate_api = _get_propagate_api()
    if propagate_api is None:
        return []

    _ensure_initialized()
    carrier: dict[str, str] = {}
    propagate_api.inject(carrier, context=_outbound_context())
    return [
        (key, value)
        for key, value in carrier.items()
        if key.lower() in _TRACE_CONTEXT_METADATA_KEYS and value
    ]


def trace_headers() -> dict[str, str]:
    """Return W3C headers for the current trace context."""
    return dict(current_trace_metadata())


def trace_env() -> dict[str, str]:
    """Return the current trace context as ``TRACEPARENT``-style env vars.

    The upper-cased W3C header names, for handing the context to a child
    process; :func:`_adopt_ambient_trace_context` reads the same spelling.
    """
    return {key.upper(): value for key, value in current_trace_metadata()}


def has_trace_metadata(metadata: list[tuple[str, str]]) -> bool:
    return any(key.lower() in _TRACE_CONTEXT_METADATA_KEYS for key, _ in metadata)


@contextlib.contextmanager
def _span(name: str) -> Generator[Any, None, None]:
    if _is_remote_tracing_off():
        yield None
        return

    _ensure_user_tracing_configured()

    with get_tracer().start_as_current_span(name) as span:
        yield span


def _default_span_name(func: Callable[..., Any]) -> str:
    return getattr(func, "__name__", _DEFAULT_PUBLIC_SPAN_NAME)


def _wrap_with_span(
    func: F,
    span_factory: Callable[[], contextlib.AbstractContextManager[Any]],
) -> F:
    """Wrap ``func`` so a span from ``span_factory`` stays open for its full execution.

    Coroutine functions, async generator functions, and generator functions must
    keep the span current while the coroutine is awaited or the generator is
    iterated, not just while the coroutine/generator object is constructed.
    """
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with span_factory():
                return await func(*args, **kwargs)

        return cast(F, async_wrapper)

    if inspect.isasyncgenfunction(func):

        @functools.wraps(func)
        async def async_gen_wrapper(*args: Any, **kwargs: Any) -> Any:
            with span_factory():
                async for item in func(*args, **kwargs):
                    yield item

        return cast(F, async_gen_wrapper)

    if inspect.isgeneratorfunction(func):

        @functools.wraps(func)
        def gen_wrapper(*args: Any, **kwargs: Any) -> Any:
            with span_factory():
                yield from func(*args, **kwargs)

        return cast(F, gen_wrapper)

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        with span_factory():
            return func(*args, **kwargs)

    return cast(F, sync_wrapper)


class _SpanContextManager(contextlib.AbstractContextManager[Any]):
    """Dual-purpose context manager / decorator returned by ``span``."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._cm: contextlib.AbstractContextManager[Any] | None = None

    def __enter__(self) -> Any:
        cm = _span(self._name)
        self._cm = cm
        return cm.__enter__()

    def __exit__(self, *exc_info: Any) -> Any:
        cm = self._cm
        self._cm = None
        assert cm is not None
        return cm.__exit__(*exc_info)

    def __call__(self, func: F) -> F:
        return _wrap_with_span(func, lambda: _span(self._name))


def span(name: str = _DEFAULT_PUBLIC_SPAN_NAME) -> _SpanContextManager:
    """Start a span and propagate its trace context to nested calls.

    Usable as a context manager (``with chalkcompute.span(...):``) or as a
    decorator (``@chalkcompute.span(...)``), including on coroutine functions
    and async generator functions.
    """
    return _SpanContextManager(name)


def _decorate_traced(func: F, name: str | None) -> F:
    span_name = name or _default_span_name(func)
    return _wrap_with_span(func, lambda: _span(span_name))


def _instrumented(name: str) -> Callable[[F], F]:
    """Trace internal SDK work without treating it as user tracing intent."""

    def decorator(func: F) -> F:
        def span_factory() -> contextlib.AbstractContextManager[Any]:
            if _is_remote_tracing_off() or not _has_tracing_intent():
                return contextlib.nullcontext()
            return get_tracer().start_as_current_span(name)

        return _wrap_with_span(func, span_factory)

    return decorator


@overload
def traced(arg: F, /) -> F: ...


@overload
def traced(arg: str | None = None, /) -> Callable[[F], F]: ...


def traced(arg: str | F | None = None) -> Callable[[F], F] | F:
    """Trace a caller function.

    Supports both ``@chalkcompute.traced`` and
    ``@chalkcompute.traced("span_name")``. Using this decorator implies tracing
    intent; if tracing has not been configured, roots sample at 1.0.
    """
    if arg is not None and not isinstance(arg, str):
        if callable(arg):
            return _decorate_traced(cast(F, arg), None)
        raise TypeError("traced expects a callable, a span name, or no argument")

    if isinstance(arg, str) and not arg.strip():
        raise ValueError("span name cannot be empty")

    def decorator(func: F) -> F:
        return _decorate_traced(func, arg)

    return decorator


@contextlib.contextmanager
def remote_function_call_span(
    function_name: str, call_type: str
) -> Generator[Any, None, None]:
    """Trace one outbound call from this process to another compute function."""
    if _is_remote_tracing_off() or not has_current_trace_context():
        yield None
        return

    otel_context = _get_context_api()
    trace_api = _get_trace_api()
    if otel_context is None or trace_api is None:
        yield None
        return

    parent_context = otel_context.get_current()
    with get_tracer().start_as_current_span(
        "chalkcompute.remote_function.call",
        kind=trace_api.SpanKind.CLIENT,
    ) as span:
        span.set_attribute("chalk.remote_function.name", function_name)
        span.set_attribute("chalk.remote_function.call_type", call_type)
        if not span.get_span_context().is_valid:
            token = otel_context.attach(parent_context)
            try:
                yield span
            finally:
                otel_context.detach(token)
            return
        yield span
