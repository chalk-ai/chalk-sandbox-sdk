Deferred results now report a call's failure exactly as the server returned
it. A failed result whose message resembles a transport error is no longer
treated as a dropped polling connection; only raised polling errors refresh the
connection and re-poll the same call ID and cursor.

Structured proxy failures from `get()`, `stream()`, and `gather()` now show the
error type, message, and call ID instead of a raw JSON envelope, and a proxy
failure that wraps a runtime error reports that error. `.remote()` errors are
unchanged.

Result collection never retries a failed call, including `ProxyError`; any
execution retries happen on the server under the same call ID.
