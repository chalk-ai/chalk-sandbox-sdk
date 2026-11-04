Client-side retries now jitter their backoff, so many clients that fail at
the same moment no longer retry in lockstep. Each wait is drawn uniformly
between zero and the current backoff step, as in gRPC's retry policy. This
applies to transport retries of RPCs that return `UNAVAILABLE`,
`ScalingGroup.call` retrying gateway 503s while a group scales from zero, and
`Sandbox.restore` retrying resume conflicts. Attempt counts, backoff caps, and
timeouts are unchanged.

Deferred result collection also uses the shared backoff. Its 60-second
unavailability budget now bounds both retry waits and polling RPCs, even
when `call_timeout=None`. Poll timeouts skip the scaling-group status lookup
so diagnostics cannot extend the result deadline. `timeout=0` still observes
the call once; subsequent polls use only the remaining budget.
