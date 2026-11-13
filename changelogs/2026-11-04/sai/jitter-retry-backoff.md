Client-side retries now jitter their backoff, so many clients that fail at
the same moment no longer retry in lockstep. Transport retries of RPCs that
return `UNAVAILABLE`, `ScalingGroup.call` retrying gateway 503s, and
`Sandbox.restore` retrying resume conflicts use the +/-20% jitter specified by
[gRPC's retry policy](https://github.com/grpc/proposal/blob/master/A6-client-retries.md#exponential-backoff).
Each wait is drawn uniformly between 0.8 and 1.2 times the exponential
backoff step, preserving the previous average delay. The maximum caps the
nominal step, including the first step; individual waits can reach 1.2 times
that maximum and are clipped to the remaining deadline when available.

Deferred result collection uses the same helper with its existing full jitter.
Both RPC retries and deferred polling honor the shared multiplier constant.
An explicit result timeout bounds retry waits and polling RPCs. Without one,
the 60-second unavailability budget limits starting another retry; an admitted
poll keeps its configured `call_timeout`, and `call_timeout=None` allows an
unbounded poll. A successful poll resets the outage budget. Poll timeouts with
a result deadline skip the scaling-group lookup so diagnostics cannot extend
the result wait; standalone polls retain it. A poll timeout is reported as such,
without substituting an earlier connection error. `timeout=0` still observes
the call once; subsequent polls use only the remaining result budget.
