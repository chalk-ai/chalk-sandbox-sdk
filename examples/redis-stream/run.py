#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["chalkcompute"]
# ///

"""
Redis Stream → RemoteFunction example.

End-to-end demo that:
  1. Deploys the ``respond`` chalkcompute.function (OpenAI chat completion)
  2. Starts a worker container that:
       - subscribes to a Redis stream ("prompts")
       - calls ``respond`` via ``RemoteFunction.from_name()``
       - writes each response to /tmp/output/
  3. Publishes a few prompts to the Redis stream (via exec into the container)
  4. Waits, then reads the responses by exec'ing into the container

Prerequisites:
  - source .env   (CHALK_CLIENT_ID, CHALK_CLIENT_SECRET, CHALK_API_SERVER, CHALK_ENVIRONMENT_ID)
  - OPENAI_API_KEY must be set locally
  - The ``fnq-redis`` service must be reachable from the container
    (it runs in the same k8s cluster: fnq-redis.ns-tmnmc9beyujew.svc.cluster.local:6379)

Usage:
    source .env
    uv run python examples/redis-stream/run.py
"""

import logging
import os
import time

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

REDIS_URL = "redis://fnq-redis.ns-tmnmc9beyujew.svc.cluster.local:6379"
STREAM = "prompts"
OUTPUT_DIR = "/tmp/output"
CONTAINER_NAME = "redis-stream-worker"

PROMPTS = [
    "What is the capital of France?",
    "Explain recursion in one sentence.",
    "Write a haiku about distributed systems.",
]

EXAMPLE_DIR = os.path.dirname(os.path.abspath(__file__))


def deploy_function() -> None:
    """Deploy the respond function (skip if already available)."""
    try:
        f = chalkcompute.RemoteFunction.from_name("respond")
        log.info("'respond' function already deployed (v%d), skipping deploy", f._version_info.version)
        return
    except chalkcompute.FunctionError:
        pass

    log.info("Deploying 'respond' function...")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "respond", os.path.join(EXAMPLE_DIR, "respond.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.respond.wait_ready(timeout=600)
    log.info("'respond' function is ready")


def stop_existing_container() -> None:
    """Stop a previous container with the same name, if any."""
    try:
        c = chalkcompute.Container.from_name(CONTAINER_NAME)
        log.info("Stopping existing container '%s'...", CONTAINER_NAME)
        c.stop()
    except chalkcompute.ContainerError:
        pass


def start_worker() -> chalkcompute.Container:
    """Start the worker container with Redis + chalkcompute installed."""
    worker_image = (
        chalkcompute.Image.debian_slim()
        .pip_install(["redis"])
        .add_local_file(
            os.path.join(EXAMPLE_DIR, "worker.py"),
            "/app/worker.py",
        )
    )

    container = chalkcompute.Container(
        name=CONTAINER_NAME,
        image=worker_image,
        entrypoint=["python", "/app/worker.py"],
        env={"REDIS_URL": REDIS_URL},
        secrets=[
            chalkcompute.Secret.from_local_env("CHALK_CLIENT_ID"),
            chalkcompute.Secret.from_local_env("CHALK_CLIENT_SECRET"),
            chalkcompute.Secret.from_local_env("CHALK_API_SERVER"),
            chalkcompute.Secret.from_local_env("CHALK_ENVIRONMENT_ID"),
            chalkcompute.Secret.from_local_env("OPENAI_API_KEY"),
        ],
        lifetime="600s",
    )

    log.info("Starting worker container...")
    container.run()
    log.info("Worker container started")
    return container


def publish_prompts(worker: chalkcompute.Container) -> None:
    """Publish prompts to the Redis stream by exec'ing into the worker container."""
    for i, prompt in enumerate(PROMPTS):
        escaped = prompt.replace("'", "\\'")
        worker.exec(
            "python", "-c",
            (
                "import redis, os; "
                "r = redis.from_url(os.environ['REDIS_URL'], decode_responses=True); "
                f"mid = r.xadd('{STREAM}', {{'text': '{escaped}'}}); "
                f"print(f'Published prompt {i}: {{mid}}')"
            ),
        )
        log.info("Published prompt %d: %s", i, prompt)


def read_results(worker: chalkcompute.Container) -> None:
    """Read result files from /tmp/output via exec."""
    result = worker.exec(
        "python", "-c",
        (
            "import os, json; "
            f"d = '{OUTPUT_DIR}'; "
            "files = sorted(f for f in os.listdir(d) if f.endswith('.txt')); "
            "print(f'Found {len(files)} result(s)'); "
            "[print(json.dumps(json.loads(open(os.path.join(d, f)).read()), indent=2)) "
            "for f in files]"
        ),
    )
    log.info("Results:\n%s", result.stdout.decode())


def main() -> None:
    # 1. Deploy the respond function.
    deploy_function()

    # 2. Stop any previous worker with the same name.
    stop_existing_container()

    # 3. Start the worker container.
    worker = start_worker()

    # 4. Give the worker a moment to connect to Redis.
    log.info("Waiting 15s for worker to initialize...")
    time.sleep(15)

    # 5. Publish prompts to Redis.
    log.info("Publishing %d prompts to stream %r...", len(PROMPTS), STREAM)
    publish_prompts(worker)

    # 6. Wait for the worker to process messages.
    log.info("Waiting 60s for worker to process messages...")
    time.sleep(60)

    # 7. Read results.
    log.info("Reading results...")
    read_results(worker)

    # 8. Clean up.
    log.info("Cleaning up...")
    try:
        worker.stop()
    except Exception:
        pass
    log.info("Done!")


if __name__ == "__main__":
    main()
