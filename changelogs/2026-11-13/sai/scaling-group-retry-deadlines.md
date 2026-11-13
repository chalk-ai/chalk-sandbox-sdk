Scaling-group cold-start retries now recompute the remaining retry budget
after refreshing status and stop before sleeping or sending another request
once it expires. Slow status refreshes no longer leave an additional stale
backoff wait, and scheduler delays do not start a request after the deadline.
An in-flight HTTP request still uses its separate per-attempt timeout.

Route readiness checks now use the shared backoff helper with centered
+/-20% jitter, preserving the average retry delay. The nominal step still
starts at 0.25 seconds and caps at 2 seconds; individual waits can reach
2.4 seconds. Both waits and probe timeouts remain clipped to the existing
20-second route-readiness budget.
