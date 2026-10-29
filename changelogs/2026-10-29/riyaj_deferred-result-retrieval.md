Deferred function result retrieval now only polls the original call. `get()`,
`stream()`, and `gather()` raise reported failures, including `ProxyError`,
without enqueueing another invocation. Successful calls continue polling through
pending and running states, and selected connection failures while reading
results still retry against the same call ID. Execution retries remain governed
by the server-side invocation or queue policy, including `function(retries=...)`.
This prevents result retrieval from initiating another execution; it does not
guarantee exactly-once execution.
