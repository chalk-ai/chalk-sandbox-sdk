"""Example: a FastAPI app exposed as a chalkcompute remote function.

The ``@chalkcompute.function(mode="fastapi", ...)`` decoration registers the
underlying ASGI app for deployment. When deployed, the chalk-remote-call
server in the container drives the app via ``process_batches_asgi``; each
incoming gRPC call carries one HTTP request as ``chalk.runtime.v1.Asgi``
messages in a binary ``asgi`` arrow column.

Run locally (no Chalk credentials needed)::

    CHALK_LOCAL=1 python -m examples.fastapi_hello.app

then::

    curl http://localhost:8080/
    curl http://localhost:8080/items/42
    curl -X POST http://localhost:8080/echo -H 'content-type: application/json' -d '{"hello":"world"}'

Deploy to Chalk::

    python -c "from examples.fastapi_hello.app import app; app.deploy().wait_ready()"
"""

from __future__ import annotations

import logging
import os

# When CHALK_LOCAL=1, skip the deploy path so the example can boot locally
# without Chalk credentials. _CHALK_REMOTE_FUNCTION is the sentinel
# chalkcompute already checks at the top of its decorator to no-op.
if os.environ.get("CHALK_LOCAL") == "1":
    os.environ.setdefault("_CHALK_REMOTE_FUNCTION", "1")

import chalkcompute
from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

_fa = FastAPI(title="chalkcompute hello-fastapi")


class _Echo(BaseModel):
    hello: str | None = None
    text: str | None = None


@_fa.get("/")
async def root() -> dict[str, str]:
    return {"message": "hello from chalkcompute.function(mode='fastapi')"}


@_fa.get("/items/{item_id}")
async def get_item(item_id: int) -> dict[str, int]:
    return {"item_id": item_id}


@_fa.post("/echo")
async def echo(payload: _Echo) -> dict[str, object]:
    return {"echoed": payload.model_dump(exclude_none=True)}


# Decoration is the same code path used for a real Chalk deploy. When
# _CHALK_REMOTE_FUNCTION is set (i.e. CHALK_LOCAL=1 above), this is a no-op
# wrapper that proxies __call__(scope, receive, send) to the FastAPI app, so
# the chalk-remote-call harness can drive it without a round trip through
# the Chalk control plane.
app = chalkcompute.function(
    _fa,
    mode="fastapi",
    name="hello-fastapi",
    cpu="500m",
    memory="512Mi",
)


if __name__ == "__main__":
    import sys

    if os.environ.get("CHALK_LOCAL") != "1":
        sys.stderr.write(
            "Refusing to run __main__ without CHALK_LOCAL=1 — running directly would\n"
            "trigger a real Chalk deploy of this function. Set CHALK_LOCAL=1 to boot\n"
            "the local HTTP harness instead, or call app.deploy() explicitly.\n"
        )
        sys.exit(2)

    # boot_function lives in chalk-remote-call-python, which chalkcompute
    # already depends on. It spawns the chalk-remote-call gRPC server in
    # mode=fastapi pointing at this module's ``app`` and runs an HTTP proxy
    # in front so curl http://localhost:8080/ exercises the whole loop.
    # Forward any extra args (e.g. --http-port 38090) to boot_function.
    from chalk_remote_call.boot_function import main as boot_main

    sys.exit(boot_main(["examples.fastapi_hello.app.app", *sys.argv[1:]]))
