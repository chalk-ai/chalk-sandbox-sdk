#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""
Redis Stream → RemoteFunction example.

End-to-end demo that:
  1. Deploys the ``respond`` chalkcompute.function (OpenAI chat completion)
  2. Starts a worker sandbox that:
       - runs Redis and subscribes to its "prompts" stream
       - calls ``respond`` via ``RemoteFunction.from_name()``
       - writes each response to /tmp/output/
  3. Publishes a few prompts to the Redis stream (via exec into the sandbox)
  4. Waits, then reads the responses by exec'ing into the sandbox

Prerequisites:
  - standard Chalk credentials in the environment or `.env`
  - OPENAI_API_KEY must be set locally
"""

import logging
import os
import time
import uuid

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

REDIS_URL = "redis://localhost:6379"
STREAM = "prompts"
OUTPUT_DIR = "/tmp/output"
SANDBOX_NAME = f"redis-stream-worker-{uuid.uuid4().hex[:8]}"
FUNCTION_NAME = f"redis-stream-respond-{uuid.uuid4().hex[:8]}"

PROMPTS = [
    "What is the capital of France?",
    "Explain recursion in one sentence.",
    "Write a haiku about distributed systems.",
]

EXAMPLE_DIR = os.path.dirname(os.path.abspath(__file__))


def deploy_function() -> chalkcompute.RemoteFunction:
    """Deploy the function bundled with this example."""
    log.info("Deploying %r function...", FUNCTION_NAME)
    import importlib.util

    os.environ["REDIS_FUNCTION_NAME"] = FUNCTION_NAME
    spec = importlib.util.spec_from_file_location(
        "respond", os.path.join(EXAMPLE_DIR, "respond.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        mod.respond.wait_ready(timeout=600)
    except Exception:
        mod.respond.delete()
        raise
    log.info("Function %r is ready", FUNCTION_NAME)
    return mod.respond


def start_worker() -> chalkcompute.Sandbox:
    """Start the worker sandbox with Redis + chalkcompute installed."""
    worker_image = (
        chalkcompute.Image.debian_slim()
        .run_commands("apt-get update -qq && apt-get install -y -qq redis-server")
        .pip_install(["redis", "chalkcompute>=2.11.1"])
        .add_local_file(
            os.path.join(EXAMPLE_DIR, "worker.py"),
            "/app/worker.py",
        )
    )

    sandbox = chalkcompute.Sandbox(
        name=SANDBOX_NAME,
        image=worker_image,
        entrypoint=[
            "bash",
            "-c",
            "redis-server --daemonize yes && exec python /app/worker.py",
        ],
        env={"REDIS_URL": REDIS_URL, "REDIS_FUNCTION_NAME": FUNCTION_NAME},
        chalk_identity=True,
        lifetime="600s",
    )

    log.info("Starting worker sandbox...")
    try:
        sandbox.run()
    except Exception:
        if sandbox.id is not None:
            sandbox.terminate()
        raise
    log.info("Worker sandbox started")
    return sandbox


def publish_prompts(worker: chalkcompute.Sandbox) -> None:
    """Publish prompts to the Redis stream by exec'ing into the worker sandbox."""
    for i, prompt in enumerate(PROMPTS):
        escaped = prompt.replace("'", "\\'")
        worker.exec(
            "python",
            "-c",
            (
                "import redis, os; "
                "r = redis.from_url(os.environ['REDIS_URL'], decode_responses=True); "
                f"mid = r.xadd('{STREAM}', {{'text': '{escaped}'}}); "
                f"print(f'Published prompt {i}: {{mid}}')"
            ),
        )
        log.info("Published prompt %d: %s", i, prompt)


def read_results(worker: chalkcompute.Sandbox) -> None:
    """Read result files from /tmp/output via exec."""
    result = worker.exec(
        "python",
        "-c",
        (
            "import os, json; "
            f"d = '{OUTPUT_DIR}'; "
            "files = sorted(f for f in os.listdir(d) if f.endswith('.txt')); "
            "print(f'Found {len(files)} result(s)'); "
            "[print(json.dumps(json.loads(open(os.path.join(d, f)).read()), indent=2)) "
            "for f in files]"
        ),
    )
    log.info("Results:\n%s", result.stdout_text)


def main() -> None:
    respond = None
    worker = None
    try:
        # 1. Deploy the respond function.
        respond = deploy_function()

        # 2. Start the worker sandbox.
        worker = start_worker()

        # 3. Give Redis and the worker a moment to initialize.
        log.info("Waiting 15s for worker to initialize...")
        time.sleep(15)

        # 4. Publish prompts to Redis.
        log.info("Publishing %d prompts to stream %r...", len(PROMPTS), STREAM)
        publish_prompts(worker)

        # 5. Wait for the worker to process messages.
        log.info("Waiting 60s for worker to process messages...")
        time.sleep(60)

        # 6. Read results.
        log.info("Reading results...")
        read_results(worker)
    finally:
        log.info("Cleaning up...")
        try:
            if worker is not None:
                worker.terminate()
        finally:
            if respond is not None:
                respond.delete()
        log.info("Done!")


if __name__ == "__main__":
    main()
