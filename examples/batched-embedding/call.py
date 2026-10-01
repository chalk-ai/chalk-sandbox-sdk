#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""Fan out a batch to the deployed ``ex-embed`` function through the public API.

This entry point requires the function defined by
``examples/embedding-function/embed.py`` to be deployed first. That deployment
intentionally persists so this separate client can call it.
"""

import logging

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

TEXTS = [
    "The quick brown fox jumps over the lazy dog.",
    "Machine learning models need high-quality training data.",
    "PyArrow is an efficient columnar data format.",
    "Chalk makes feature engineering simple and fast.",
    "Embeddings capture semantic meaning as dense vectors.",
    "Real-time fraud detection requires low-latency inference.",
    "Distributed systems improve throughput via parallelism.",
    "The cat sat on the mat and watched the birds.",
    "Transformers revolutionized natural language processing.",
    "Feature stores bridge the gap between training and serving.",
    "Vector databases enable fast nearest-neighbor search.",
    "Batch processing amortizes per-request overhead.",
    "Kubernetes orchestrates containerized workloads at scale.",
    "Data pipelines must be both reliable and observable.",
    "Cosine similarity measures the angle between two vectors.",
    "Tokenization splits text into subword units for models.",
    "gRPC provides efficient binary serialization over HTTP/2.",
    "Prompt engineering shapes how large language models respond.",
    "Time-series data requires specialized storage and indexing.",
    "Good abstractions hide complexity without losing control.",
]


def main() -> None:
    embed = chalkcompute.RemoteFunction.from_name("ex-embed")
    embed.wait_ready()
    log.info("Embedding %d texts concurrently", len(TEXTS))
    embeddings = chalkcompute.gather([embed.defer(text) for text in TEXTS], timeout=120)
    print(f"Embedded {len(embeddings)} texts ({len(embeddings[0])} dimensions each)")


if __name__ == "__main__":
    main()
