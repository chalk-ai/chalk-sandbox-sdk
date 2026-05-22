# fastapi-hello

A tiny FastAPI app deployed as a chalkcompute remote function via `mode="fastapi"`.

## Local boot

No Chalk credentials needed. From the repo root:

```bash
CHALK_LOCAL=1 python -m examples.fastapi_hello.app
```

This sets `_CHALK_REMOTE_FUNCTION=1` before importing chalkcompute, so the
decorator becomes a no-op wrapper around the FastAPI instance. The
`__main__` block then hands off to `chalk_remote_call.boot_function`, which
spawns the chalk-remote-call gRPC server in `mode=fastapi` pointing at this
module's `app`, and runs an HTTP proxy in front of it on `localhost:8080`.

In another shell:

```bash
curl http://localhost:8080/
curl http://localhost:8080/items/42
curl -X POST http://localhost:8080/echo \
     -H 'content-type: application/json' \
     -d '{"hello":"world","text":"hi"}'
curl http://localhost:8080/docs   # FastAPI's auto-generated Swagger UI
```

Each request travels: HTTP → `chalk.runtime.v1.Asgi` proto (HttpScope + HttpRequest)
→ binary `asgi` arrow column → gRPC `CallFunction` → `process_batches_asgi`
→ FastAPI app → response `Asgi` messages → HTTP response. Identical wire
format to what runs in the deployed container.

## Deploy

```bash
python -c "from examples.fastapi_hello.app import app; app.deploy().wait_ready()"
```

Once deployed, requests are dispatched through the Chalk control plane to a
scaling-group replica running `chalk-remote-call --mode fastapi` against the
same `app` object.
