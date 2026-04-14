"""Embed a batch of texts using chunked_call for parallel execution.

This example assumes the ``embed`` function from
``examples/embedding-function/embed.py`` is already deployed.

Prerequisites:
  1. Deploy the embed function:  uv run python examples/embedding-function/embed.py
  2. Ensure CHALK_CLIENT_ID / CHALK_CLIENT_SECRET / CHALK_API_SERVER / CHALK_ENVIRONMENT_ID are set.
  3. Port-forward the fnq-server:
       kubectl port-forward svc/fnq-server 50051:50051 -n <namespace>

Usage:
    source ~/chalk/chalk-sandbox-sdk/.env
    cd ~/chalk/chalk-private/dataframe && uv run python ~/chalk/chalk-sandbox-sdk/examples/batched-embedding/call.py
"""

import logging
import time

import pyarrow as pa

from chalkdf.remote import AsyncFunction

from libchalk.runtime.remote_call import AsyncRemoteCallClient

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
    # Connect to the function queue server (port-forwarded to localhost:50051).
    client = AsyncRemoteCallClient.create("localhost:50051")
    embed = AsyncFunction(client, "embed", pa.large_list(pa.float32()), None)
    args = pa.table({"text": pa.array(TEXTS, type=pa.large_utf8())})
    log.info("Embedding %d texts with chunk_size=5, concurrent_chunks=4 ...", len(args))
    result = embed.chunked_call(args, chunk_size=5, concurrent_chunks=4, timeout=120)

    print(result)



if __name__ == "__main__":
    main()
