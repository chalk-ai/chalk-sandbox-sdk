# Function tracing

Detailed client and worker stages are opt-in. A function deployed without a
`tracing=` argument follows a sampled caller trace but does not create traces
for ordinary requests (`parentbased_always_off`). This also applies to classes.
Existing deployments retain their policy until redeployed.

Trace a selected request using the existing caller-side API:

```python
import chalkcompute as cc

fn = cc.RemoteFunction.from_name("rr-go-brr")
with cc.span("investigate-one-call"):
    result = fn.remote(100, 232)

with cc.span("investigate-queued-call"):
    handle = fn.defer(100, 232)
    result = handle.get()
```

`cc.span` can also be used as a decorator, including on async functions;
`@cc.traced` is another supported decorator. Caller tracing needs an exporter
configured to retain client spans. The trace context is propagated even if the
caller has no exporter. There is no reserved per-call `tracing=True` argument:
arguments to `.remote()` and `.defer()` are arguments to the user function.

To trace unparented requests inside the function, explicitly deploy with
`@cc.function(tracing=True)` or `tracing=cc.TracingPolicy(sample_rate=0.01)`.
Parent-based sampling honours an existing caller decision; `sample_rate`
controls unparented root requests. Deploying with `tracing=False` disables
worker tracing even when a caller supplies a sampled parent. It does not disable
tracing in the caller or DP API.

Detailed SDK spans check the active parent's sampled bit before obtaining a
tracer or constructing a span. Worker and DP/queue spans similarly gate detailed
stages before constructing spans. These gates reduce unsampled bookkeeping;
context propagation, the gate checks, and existing request logging still cost
work. Explicit root sampling requires a root sampling decision. Traced requests
still pay recording/export costs, and a sampled caller in a batch traces the
shared batch execution. Unsampled callers keep their original context and do
not acquire detailed per-caller spans because a batch sibling was sampled.

A full DP waterfall requires sampled context supplied by the caller: enabling
worker root tracing alone cannot retroactively trace an upstream DP request.
