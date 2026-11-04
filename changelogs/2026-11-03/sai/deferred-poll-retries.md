`get()`, `stream()`, and `gather()` now keep re-polling a deferred call when the
polling connection drops repeatedly, instead of failing after a single
reconnect. Re-polls use jittered exponential backoff capped at 5 seconds, so
handles collected together with `gather()` do not retry in lockstep. Retries
re-poll the same call ID and cursor, never submit another invocation, and stop
at the `timeout`, or after 60 seconds of continuous unavailability when no
`timeout` is given.

The `timeout` passed to `get()`, `stream()`, or `gather()` now also bounds each
poll. Previously a hung poll could wait for the function's full `call_timeout`
(60 seconds by default) past the requested `timeout`.
