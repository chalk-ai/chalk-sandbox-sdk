Deferred results now report a call's failure exactly as the server returned
it. A failed result whose message resembles a transport error is no longer
treated as a dropped polling connection; only raised polling errors refresh the
connection and re-poll the same call ID and cursor. The native client now raises
those errors when a poll's connection drops, instead of reporting them as a
failed call.

Structured proxy failures from `get()`, `stream()`, and `gather()` now show the
error type, message, and call ID instead of a raw JSON envelope, and a proxy
failure that wraps a runtime error reports that error. `.remote()` errors are
unchanged.

Result collection never retries a failed call, including `ProxyError`; any
execution retries happen on the server under the same call ID.

A cancelled call now raises `FunctionError` from `get()`, `stream()`, and
`gather()` instead of polling until the timeout, or indefinitely without one.

The updated native client also retries `.remote()` calls when a request fails to
send or its response body fails to read.
