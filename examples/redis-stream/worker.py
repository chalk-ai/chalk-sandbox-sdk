"""Worker that subscribes to a Redis stream and dispatches to a remote function.

This script runs *inside* a chalkcompute Container. It:
  1. Connects to Redis (REDIS_URL env var)
  2. Reads messages from the ``prompts`` stream via XREADGROUP
  3. Calls the deployed ``respond`` chalkcompute.function via RemoteFunction.from_name()
  4. Writes each response to /tmp/output/<message_id>.txt
"""

import json
import logging
import os

import redis
import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

STREAM = "prompts"
GROUP = "workers"
CONSUMER = f"worker-{os.getpid()}"
OUTPUT_DIR = "/tmp/output"

REDIS_URL = os.environ["REDIS_URL"]


def ensure_group(r: redis.Redis) -> None:
    """Create the consumer group if it doesn't exist."""
    try:
        r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        log.info("Created consumer group %s on stream %s", GROUP, STREAM)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise
        log.info("Consumer group %s already exists", GROUP)


def main() -> None:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    ensure_group(r)

    # Look up the deployed ``respond`` function by name.
    respond = chalkcompute.RemoteFunction.from_name("respond")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log.info("Worker ready — listening on stream %r", STREAM)

    while True:
        entries = r.xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=1, block=5000)
        if not entries:
            continue

        for _stream_name, messages in entries:
            for msg_id, fields in messages:
                text = fields.get("text", "")
                log.info("Received message %s: %s", msg_id, text[:80])

                try:
                    result = respond.remote(text)
                    log.info("Got response for %s (len=%d)", msg_id, len(str(result)))

                    safe_id = msg_id.replace("-", "_")
                    out_path = os.path.join(OUTPUT_DIR, f"{safe_id}.txt")
                    with open(out_path, "w") as f:
                        f.write(json.dumps({"prompt": text, "response": str(result)}))
                    log.info("Wrote result to %s", out_path)
                except Exception:
                    log.exception("Failed to process message %s", msg_id)

                r.xack(STREAM, GROUP, msg_id)


if __name__ == "__main__":
    main()
