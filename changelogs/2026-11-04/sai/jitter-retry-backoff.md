Client-side retries now jitter their backoff, so many clients that fail at
the same moment no longer retry in lockstep. Transport retries of RPCs that
return `UNAVAILABLE`, `ScalingGroup.call` retrying gateway 503s, and
`Sandbox.restore` retrying resume conflicts use the same +/-20% jitter as
gRPC. Each wait is drawn uniformly between 0.8 and 1.2 times the exponential
backoff step, preserving the previous average delay. The maximum caps the
nominal step, including the first step; individual waits can reach 1.2 times
that maximum and are clipped to the remaining deadline when available.

Deferred result collection retains its existing full jitter. Its 60-second
unavailability budget now bounds both retry waits and polling RPCs, even
when `call_timeout=None`. Poll timeouts skip the scaling-group status lookup
so diagnostics cannot extend the result deadline. `timeout=0` still observes
the call once; subsequent polls use only the remaining budget.
