# Propagating trace context into evaluation scorers

Status: the SDK half is implemented — `chalkcompute/_trace.py`, session
stamping in `_tracing.py` / `_call_context.py`, the nullability fix, the
strip-mode annotation guard, and the monitoring-read permission request. The
runner half (chalk-private) and the shim `force_flush` are follow-ups; see
"Blockers" at the end.

Question: how does an evaluation scorer get at the telemetry of the task
invocation it is scoring?

```python
@cc.function
def exact_match(expected: str, output: str, trace: cc.Trace) -> float:
    spans = trace.get_spans()
    return sum(s.attribute_float("llm.cost") or 0.0 for s in spans)
```

## Decision: `Trace` is the name, a session is the thing

The public noun is `cc.Trace`. Underneath it holds a **session id**, and every
query it makes is session-scoped. The name is a deliberate misnomer: what a
scorer means by "the trace" is the whole evaluated unit of work — every turn,
every nested LLM call — which in the telemetry model is a session, not an OTel
trace.

chalk-private#41891 is what makes this the better-founded choice rather than a
naming preference. Three reasons:

1. **The correlation key becomes caller-minted.** #41891 adds
   `catalog_call('fn', x, metadata => {'x-chalk-session-id': s})`, carrying
   caller-supplied gRPC headers into the callee, plus `uuid_string()` to mint a
   fresh v4 UUID per row. The evaluation runner therefore knows the key *before*
   it makes the call, and can write the same value into the task's header and
   into the scorer's `trace` column. No round trip through the callee.
2. **It survives multi-turn agents; a trace id cannot.** An agent SDK that starts
   a new root trace per turn produces traces that are siblings, not descendants,
   of the catalog-call trace. A caller-minted session id is exactly the key
   chalk-private#41833 built `otel.otel_traces.SessionId` to group on. Modelling
   on a trace id would work for a single-turn task and quietly under-report for
   anything agentic.
3. **`x-chalk-session-id` is already the blessed key**, named in #41891's summary
   and in the module docstring of `chalkcompute/_call_context.py` (merged in
   chalk-sandbox-sdk#302).

So the original instinct about `SearchSessions` was right, with one correction:
it is the *summary* RPC, not the span RPC.

| `Trace` method | RPC |
|---|---|
| `get_spans()` | `ListSpan(query="session:<id>")` |
| `summary()` | `SearchSessions(session_ids=[id])` → `ChalkSessionSummary` |
| `traces()` | `SearchTraceSummaries(session_ids=[id])` |

`ListSpan` has no `session_ids` field, but #41833 registered `session` /
`session_id` as a span search key (`go-api-server/traces/searchfilter.go`,
`SessionId = ?`), so the faceted `query` string is the supported path.
`SearchSessions` returns aggregates only — `session_id`, start/end, `duration`,
`trace_count`, `span_count`, `first_root_span_name` — and no spans at all.

## Why not ambient context

`cc.current_trace()` with no parameter is not implementable, for two structural
reasons rather than stylistic ones:

1. The runner is ChalkSQL. chalk-private#41747 compiles an evaluation into a
   query that invokes `catalog_call('function.<version-id>', ...)`, binding
   arguments to columns by name. For the scorer, a column is the only channel in.
2. Even if there were a side band, the scorer's own OTel context describes *the
   scorer's* trace. The interesting one is the task's. Ambient context would
   silently return the wrong thing.

The value has to arrive as a bound argument. The only open question is its type.

## Recommendation: `trace: cc.Trace`, with `trace: str` wire-identical

```python
def exact_match(expected: str, output: str, trace: cc.Trace) -> float:
    spans = trace.get_spans()                       # recommended

def exact_match(expected: str, output: str, trace: str) -> float:
    spans = cc.Trace.from_id(trace).get_spans()     # equivalent, explicit

cc.Trace.from_id("0f9a...").get_spans()             # notebook, CI, ad-hoc
```

`chalkcompute/_arrow_schema.py:279` maps `str` to `ArrowType(large_utf8=...)`.
`Trace` opts into the Arrow contract in `chalkcompute/_arrow_serde.py` declaring
the same `large_utf8`:

```python
class Trace:
    """A session's worth of telemetry: every trace and span under one evaluated call."""

    __chalk_arrow_type__ = arrow_pb2.ArrowType(large_utf8=arrow_pb2.EmptyMessage())

    def __init__(self, id: str, *, client: TraceClient | None = None) -> None: ...

    def __chalk_serialize__(self) -> pa.Scalar:
        return pa.scalar(self.id, type=pa.large_utf8())

    @classmethod
    def __chalk_deserialize__(cls, value: pa.Scalar) -> Trace:
        return cls(value.as_py())
```

Both annotations therefore emit an identical `large_utf8` column in the
function's `inputArrowSchema`, which is what the runner binds. `Trace` costs
nothing on the wire and nothing on the server; it buys a discoverable method
surface and a type that documents what the string is. The contract machinery
already exists — `has_chalk_arrow_contract`, the two dunders, and the per-column
decoder in `handler_runtime_source()` — so this is the mechanism it was built
for.

Server-side, `syntheticScorerArguments = {"output", "trace"}` is already merged
(`go-api-server/evaluation/module.go:37`), exempting `trace` from the
dataset-column match in `validateFunctionArguments`.

## The missing link: nothing stamps `session.id` on spans yet

This is the one net-new piece of work, and it is load-bearing — without it
nothing else in this design produces a non-empty result.

### Why the header alone does nothing

chalk-sandbox-sdk#302 puts the caller's headers into a contextvar
(`set_call_scope` → `get_call_context()`). Span *export* is a completely separate
pipeline: the user's code creates spans through a tracer, the SDK
`TracerProvider` batches them, and an OTLP exporter ships them to a node-local
collector (`OTEL_EXPORTER_OTLP_ENDPOINT=http://$(HOST_IP):<port>`, injected by
`go-api-server/builder/env_vars.go:68`). Nothing joins the two. A header sitting
in a contextvar is invisible to every span the process emits.

And #41833 computes `otel.otel_traces.SessionId` from a `DEFAULT` expression over
the span's own attribute maps, evaluated at write time. If no span carries any of
the six recognized spellings, `SessionId` is `''`, the bloom-filter skip index
matches nothing, and both `ListSpan(query="session:...")` and `SearchSessions`
return empty. There is no later opportunity to attach the id.

So the id has to be **on the spans**, before export.

### Why it must be a span attribute, not a resource attribute

Resource attributes are the obvious-looking shortcut and they are wrong twice
over:

- **Lifetime.** `chalkcompute/_tracing.py:91` builds the `Resource` once, at
  provider construction: `Resource.create({"service.name": "chalkcompute"})`. A
  resource is immutable and process-wide. A function pod serves rows from many
  different sessions over its life, so a process-wide value cannot express
  "this row's session".
- **Precedence.** In #41833's coalesce order, resource-attribute `session.id` is
  rank 6 — last. Span-attribute `session.id` is rank 1.

### Why we cannot just set it on our own spans

The spans that matter are not ours. `llm.cost` is emitted by the user's LLM
instrumentation — OpenInference, `openinference-instrumentation-*`, an agent
SDK's own tracer — which creates spans through its own `Tracer` obtained from the
global provider. `chalkcompute` never sees those `start_span` calls and cannot
decorate them.

What *is* global is the provider. A `SpanProcessor` registered on it receives
`on_start` for every span created by every tracer in the process, whoever owns
the instrumentation. That is the only hook with the right reach.

### The mechanism

Implemented as `enter_session` / `exit_session` / `current_session_id` in
`chalkcompute/_tracing.py`, driven from `set_call_scope` in
`chalkcompute/_call_context.py` — so all three handler shims (function, batched,
`@cls`) get it without a shim change.

Two halves. First, publish the session id into the OTel context for the row's
duration, alongside the existing `set_call_scope`:

```python
# in the shim's per-row scope, when x-chalk-session-id is present
from opentelemetry import baggage, context as otel_context

ctx = baggage.set_baggage("session.id", session_id)
token = otel_context.attach(ctx)
try:
    ...  # call the user's function for this row
finally:
    otel_context.detach(token)
```

Second, a provider-level processor that copies it onto every span at creation:

```python
class _SessionIdSpanProcessor(SpanProcessor):
    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        session_id = baggage.get_baggage("session.id", context=parent_context)
        if session_id:
            span.set_attribute("session.id", str(session_id))
```

Read from the passed `parent_context`, not `context.get_current()` — `on_start`
is called with the parent context explicitly, and they differ.

`on_start` is the correct hook rather than `on_end`: attributes must be set while
the span is recording, and setting them at start means samplers and downstream
processors see the value too.

### Why baggage rather than a plain contextvar

In-process, a contextvar would work and is simpler. Baggage buys one thing a
contextvar cannot: **it crosses process boundaries for free.** Baggage is
serialized by `W3CBaggagePropagator`, which is a global default (verified: the
default composite propagator is `TraceContextTextMapPropagator` +
`W3CBaggagePropagator`, fields `traceparent`, `tracestate`, `baggage`), and
`chalkcompute/_tracing.py:24` already whitelists `baggage` in
`_TRACE_CONTEXT_METADATA_KEYS`, which `current_trace_metadata()` emits on every
outbound call.

So a task that fans out to nested `chalkcompute` functions, or to a scaling
group, carries the session with it and their spans join the same session with no
additional plumbing. That is precisely the multi-turn / multi-hop case a trace id
could not express.

For a sandbox running its own agent process, the propagation is not automatic —
pass `trace.id` in explicitly (Chalk's opencode plugin already sets `session.id`
directly on its spans, per #41833's table, so it will land in the same session).

### Installation and precedence details

- **Install before spans exist.** The processor must be on the provider before
  the first span it should stamp. Adding it inside `_install_tracer_provider`
  (`_tracing.py:92-106`) covers the case where chalkcompute owns the provider.
- **Respect a user-installed provider.** `_install_tracer_provider` returns early
  when `_provider is not None`, and an application may have installed its own
  `TracerProvider` before chalkcompute initialized. In that case add the
  processor to `trace_api.get_tracer_provider()` if it exposes
  `add_span_processor`, and no-op otherwise (the API's no-op provider does not).
- **Cost.** `on_start` runs for every span in the process: one baggage lookup,
  and one `set_attribute` only when a session is active. Negligible, but it is
  not free — install lazily the first time a session id is observed if that
  matters.
- **Sampling.** `ParentBased(TraceIdRatioBased(rate))` means a root sampled off
  produces no spans at all, and an empty session. #41891 notes caller-supplied
  headers now win over the trace context the caller's process injects, so the
  runner can send an explicit `traceparent` with the sampled flag set and
  `ParentBased` will honor it — a clean way to force evaluation traces to be
  sampled regardless of the deployed function's `TracingPolicy`. The other
  precondition is that the function is not deployed with
  `CHALK_REMOTE_FUNCTION_TRACE_POLICY=always_off`
  (`go-api-server/externalfunctioncatalog/module.go:149`), which short-circuits
  span creation entirely.

## API sketch

```python
@dataclass(frozen=True)
class Span:
    span_id: str
    trace_id: str
    parent_span_id: str | None
    operation_name: str
    start_time: datetime
    end_time: datetime
    duration: timedelta
    status_code: SpanStatusCode
    kind: SpanKind
    attributes: Mapping[str, str]           # ChalkSpan.attributes is map<string,string>
    resource_attributes: Mapping[str, str]

    def attribute_float(self, key: str) -> float | None: ...
    def attribute_int(self, key: str) -> int | None: ...


class Trace:
    id: str                                  # a session id

    @classmethod
    def from_id(cls, session_id: str, *, chalk_client: Any | None = None) -> Trace: ...

    def get_spans(
        self,
        *,
        operation_name: str | None = None,
        attribute_filters: Mapping[str, str | None] | None = None,
        limit: int | None = None,
        wait: bool = True,          # default: wait for ingest to settle
        timeout: float = 60.0,      # raises TimeoutError if still unsettled
        expect_min: int | None = None,
    ) -> list[Span]: ...

    def summary(self) -> TraceSummary: ...   # SearchSessions(session_ids=[id])
    def traces(self) -> list[TraceSummary]: ...  # the individual turns


class TraceClient:
    """Mirrors EvaluationClient: Connect-JSON over ConnectClient.rpc."""
    # chalk.server.v1.TraceService / ListSpan, SearchSessions, SearchTraceSummaries
```

`TraceClient` follows `_evaluation.py` exactly — `ConnectClient.rpc(service,
method, request)`, `@_instrumented(...)` on each public method, transparent
`page_token` paging in the shape of `EvaluationClient.list_runs`. Nothing new is
needed in `_gen/`; the evaluation SDK already talks Connect JSON without
generated stubs.

### `attributes` is `map<string, string>`

`ChalkSpan.attributes` is a string map, so `s["llm.cost"]` yields `"0.0012"` and
`sum(...)` over it raises `TypeError: unsupported operand type(s) for +: 'int'
and 'str'`. Hence `attribute_float` / `attribute_int` rather than raw indexing.
It is the first thing anyone writing a cost scorer will hit.

### `wait` semantics on `get_spans`

Spans reach ClickHouse via `BatchSpanProcessor` → OTLP collector →
`otel.otel_traces`. The scorer runs immediately after the task inside the same
ChalkSQL query. Two facts make a naive read unreliable:

- `chalkcompute/_tracing.py:103` installs a plain `BatchSpanProcessor` (5s
  default schedule delay) and nothing in the SDK ever calls `force_flush`.
  `atexit.register(provider.shutdown)` only fires at process exit, and a function
  pod persists across invocations — so nothing flushes between the task returning
  and the scorer running.
- Ingest into ClickHouse is asynchronous on top of that.

So a single read can return `[]`, or worse a *partial* list, which silently
under-reports a cost sum rather than failing.

**Waiting is therefore the default, not an opt-in.** A scorer that gets this
wrong produces a plausible number, not an error, so the safe behavior has to be
what you get for free:

- `wait=True` (default) polls until the span count is stable across two
  consecutive polls, then returns.
- `timeout=60.0` bounds that; exceeding it raises `TimeoutError` rather than
  returning a partial list. Failing an evaluation row is recoverable; a silently
  wrong score is not.
- `wait=False` is the explicit opt-out, for callers reading a long-finished
  session from a notebook where there is nothing to wait for.
- `expect_min=n` turns a known lower bound into a hard check — the strongest
  guarantee available, for scorers that know the shape of what they are scoring.

Count-stability is pragmatic, not a proof: it cannot distinguish "complete" from
"quiet", so a long gap between two agent turns can settle early. That is the
argument for `expect_min` where the caller knows better, and for documenting the
limit plainly.

A `force_flush` after each invocation in the generated worker shim would shrink
the window substantially and is worth doing regardless — it converts the
unbounded `BatchSpanProcessor` delay into just the collector-to-ClickHouse hop.

### Nullability

`rich_to_arrow_schema` checks `has_chalk_arrow_contract(typ)` *before*
`_unwrap_optional` (`chalkcompute/_arrow_schema.py:131-140`), so today a bare
contract type is emitted `nullable=False`, and `Trace | None` falls past the
contract check into `_rich_to_arrow_type(Trace)`, which does not know the type
and raises. The `trace` column is null until the runner is wired up, and stays
nullable for tasks that emit no telemetry. Move the contract check after
`_unwrap_optional` and propagate the unwrapped `nullable`. The runtime decoder is
already null-safe — `_chalk_column_decoder` yields `None` for invalid scalars —
so `trace: cc.Trace | None` works end to end once the schema builder agrees.

## Runner-side consequences of #41891

**Header to the task, column to the scorer — the scorer signature is unchanged.**
#41891 adds two channels, and they do different jobs here. This is a
query-codegen constraint, invisible in the SDK surface:

| channel | who gets it | what it does |
|---|---|---|
| `metadata => {'x-chalk-session-id': s}` | the **task** call | stamps the task's spans, so they land in the session |
| an ordinary bound argument named `trace` | the **scorer** call | the value the scorer queries with |

The scorer still declares `trace: cc.Trace` and receives it as a normal function
argument. "Column" here means only "an argument bound from a query column, as
`output` already is" — *not* something derived from the dataset's input columns,
and not `get_call_context()`. The generated query:

```sql
WITH rows AS (
  SELECT *, uuid_string() AS trace FROM <dataset revision>
)
SELECT
  trace,
  catalog_call('function.<task-version>', raw_title,
               metadata => {'x-chalk-session-id': trace}) AS output,
  catalog_call('function.<scorer-version>', expected, output, trace) AS "<scorer-id>"
FROM rows
```

The scorer call deliberately has **no** `metadata =>`. If it did, the scorer's own
spans would be stamped with the same session id, and an LLM-as-judge scorer would
find its own `llm.cost` spans inside `trace.get_spans()` — inflating the very
number it is computing. If you later want judge cost attributed, mint a second id
for the scorer call rather than reusing the task's.

**Header granularity costs one request per row.** #41891 splits a batch into
separate requests when a header expression's value differs per row, pinned by
`test_per_row_uuid_metadata_fans_out_one_call_per_row`. A per-row session is
inherent — a scorer wants *that row's* spans — so an N-row dataset means N RPCs.
chalk-private#41786's `catalog_call_with_metadata` already pays exactly that cost
(it forces one-row RPCs to get a fresh trace), so this is no regression. The
batching path is `row_metadata =>`, which gives per-row granularity *inside* one
request; it costs inventing a blob convention and a second stamping path, so it
belongs to a later scale pass, not v1.

**The uuid must be materialized in a CTE.** #41891 notes that the
lateral-column-alias spelling
(`select uuid_string() as s, catalog_call(..., metadata => {'k': s})`) needs a
separate in-flight change, and its own tests use a CTE. The generated eval query
must therefore bind the uuid once in a CTE and reference it both in the task
call's header and in the persisted `trace` column.

**#41786 stops being a prerequisite.** Its `metadata.trace_id` was a way to get a
row-level key *back* from the callee. A caller-minted session id needs no round
trip. chalk-private#41787's plumbing — persist a `trace` column, bind it to
scorers declaring the synthetic argument, register the column on the result
dataset — is still needed; only the source of the value changes.

## Blockers, in dependency order

1. **Function pods cannot call `TraceService` at all.**
   `rust/chalkcompute_native/src/lib.rs:243-253` hardcodes the exchange token's
   requested permissions to `Authenticated, QueryOnline, QueryOffline,
   QueryOfflineRead, DeployRead, DeployCreate`. Every `TraceService` RPC requires
   `PERMISSION_MONITORING_READ` (`= 6`), which is absent. Needs
   `Permission::MonitoringRead` added plus rebuilt native wheels — **and**
   confirmation that the auth server actually grants `MonitoringRead` on a
   function-pod exchange token. Verify that grant policy server-side rather than
   assuming the request suffices. This blocks `trace: str` exactly as much as
   `trace: cc.Trace`, so it is not a discriminator between them.

2. **Span stamping** (the section above). Without it every `get_spans()` is empty.

3. **Strip mode, two separate failures.**
   `_local_dev.should_strip_source()` is true whenever the installed
   `chalkcompute` reports version `0.0.0` — every deploy from this source
   checkout.
   - *Loud:* `_strip_chalkcompute_from_source` walks `ast.iter_child_nodes`, so it
     removes only top-level chalkcompute imports and never rewrites annotations.
     The deployed module keeps `trace: cc.Trace` while losing
     `import chalkcompute as cc`, and the worker shim's `get_type_hints(_target)`
     raises `NameError: name 'cc' is not defined` in-container. The existing
     guard misses it: `_assert_stripped_source_self_contained` inspects only
     statements in `top.body`, and annotations live on the `FunctionDef`'s args.
     v1 fix: extend that guard to walk entrypoint annotations so it fails at
     deploy with the existing actionable message. Later: embed a minimal `Trace`
     into `handler_runtime_source()` and rewrite the annotation in the stripper —
     though rewriting alone is not enough, since a body calling
     `trace.get_spans()` still needs a real `Trace`, which is why the guard is
     the honest v1.
   - *Silent, and worse:* the shim's `_scope` helper swallows `ImportError` and
     returns `None` (`chalkcompute/_function.py` at origin/main), so in a
     stripped image `get_call_context()` is always empty. No session id is
     stamped, `get_spans()` returns nothing, and no error is raised anywhere.

   Published-SDK deploys — the customer path — install the SDK into the image and
   are unaffected by both.

4. **#41891 must land**, then the runner change described above, then the
   nullability fix. The smoke test cannot pass end to end before 1–4.
