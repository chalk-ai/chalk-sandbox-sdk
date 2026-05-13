"""End-to-end test for the embedding-function example.

Deploys embed and orchestrate functions, then verifies the orchestrate
function can call embed and return embeddings.
"""

import asyncio
import os
import uuid

import chalkcompute

from e2etests.conftest import _deploy_script, needs_staging

RUN_ID = uuid.uuid4().hex[:8]


@needs_staging
def test_orchestrate() -> None:
    """Deploy embed + orchestrate, then call orchestrate and verify results."""
    embed_name = f"test_embed_orchestrate_{RUN_ID}"
    orchestrate_name = f"test_orchestrate_{RUN_ID}"
    os.environ["CHALK_EMBED_FUNCTION_NAME"] = embed_name
    os.environ["CHALK_ORCHESTRATE_FUNCTION_NAME"] = orchestrate_name
    _deploy_script("embedding-function", "embed.py")
    _deploy_script("embedding-function", "orchestrate.py")

    orchestrate = chalkcompute.RemoteFunction.from_name(orchestrate_name).wait_ready()
    embeddings = orchestrate(["Hello", "world!"])

    assert len(embeddings) == 2, f"Expected 2 embeddings, got {len(embeddings)}"
    for i, emb in enumerate(embeddings):
        assert isinstance(emb, list), f"Embedding {i} is not a list"
        assert len(emb) == 1536, f"Embedding {i} has {len(emb)} dims, expected 1536"
        assert all(isinstance(v, float) for v in emb), (
            f"Embedding {i} has non-float values"
        )


@needs_staging
def test_embed_direct_call() -> None:
    """Deploy embed, then call it directly from a local client."""
    embed_name = f"test_embed_direct_{RUN_ID}"
    os.environ["CHALK_EMBED_FUNCTION_NAME"] = embed_name
    _deploy_script("embedding-function", "embed.py")

    fn = chalkcompute.RemoteFunction.from_name(embed_name).wait_ready()
    embedding = fn("hello")

    assert isinstance(embedding, list), f"Expected list, got {type(embedding).__name__}"
    assert len(embedding) == 1536, f"Got {len(embedding)} dims, expected 1536"
    assert all(isinstance(v, float) for v in embedding), "Non-float values in embedding"


@needs_staging
def test_async_embed_direct_call() -> None:
    """Deploy async_embed, then call it with await."""
    async_embed_name = f"test_async_embed_{RUN_ID}"
    os.environ["CHALK_ASYNC_EMBED_FUNCTION_NAME"] = async_embed_name
    _deploy_script("embedding-function", "embed_async.py")

    async def _run() -> list[float]:
        fn = chalkcompute.RemoteFunction.from_name(async_embed_name).wait_ready()
        return await fn("hello")

    embedding = asyncio.run(_run())

    assert isinstance(embedding, list), f"Expected list, got {type(embedding).__name__}"
    assert len(embedding) == 1536, f"Got {len(embedding)} dims, expected 1536"
    assert all(isinstance(v, float) for v in embedding), "Non-float values in embedding"
