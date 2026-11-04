Client-side retries now jitter their backoff, so many clients that fail at
the same moment no longer retry in lockstep. Each wait is drawn uniformly
between zero and the current backoff step, as in gRPC's retry policy. This
applies to transport retries of RPCs that return `UNAVAILABLE`,
`ScalingGroup.call` retrying gateway 503s while a group scales from zero, and
`Sandbox.restore` retrying resume conflicts. Attempt counts, backoff caps, and
timeouts are unchanged.
