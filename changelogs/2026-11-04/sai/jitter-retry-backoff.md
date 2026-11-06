Client-side retries now jitter their backoff, so many clients that fail at
the same moment no longer retry in lockstep. Transport retries of RPCs that
return `UNAVAILABLE` use full jitter, drawing each wait uniformly between
zero and the current backoff step. `ScalingGroup.call` retrying gateway 503s
and `Sandbox.restore` retrying resume conflicts draw between 0.5 and 1.5 times
the step, preserving its expected wait rather than doubling retry traffic.
The maximum caps the nominal step, including the first step; individual
mean-preserving waits can reach 1.5 times that maximum and are clipped to the
remaining deadline.

Deferred result collection also uses the shared backoff. Its 60-second
unavailability budget now bounds both retry waits and polling RPCs, even
when `call_timeout=None`. Poll timeouts skip the scaling-group status lookup
so diagnostics cannot extend the result deadline. `timeout=0` still observes
the call once; subsequent polls use only the remaining budget.
