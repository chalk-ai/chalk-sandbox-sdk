Add `chalkcompute.openai_client()` for synchronous OpenAI-compatible requests
through Chalk's model gateway. Install `chalkcompute[gateway]` and use ambient
Chalk credentials, or `chalk_identity=True` in a deployed function. The client
refreshes credentials and propagates the current trace and session per request,
without importing private scorer helpers or changing OpenAI retry/timeout defaults.
