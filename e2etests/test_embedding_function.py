"""End-to-end test for the embedding-function example.

Deploys embed and orchestrate functions, then verifies the orchestrate
function can call embed and return embeddings.
"""

import asyncio

import chalkcompute
from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import (
    _delete_function,
    _deploy_script,
    needs_openai,
    needs_staging,
)


@needs_staging
@needs_openai
def test_orchestrate() -> None:
    """Deploy embed + orchestrate, then call orchestrate and verify results."""
    embed_name = build_scoped_name("ex-embed-orchestrated")
    orchestrate_name = build_scoped_name("ex-orchestrate")
    try:
        _deploy_script(
            "embedding-function", "embed.py", function_name=embed_name
        )
        _deploy_script(
            "embedding-function",
            "orchestrate.py",
            function_name=orchestrate_name,
            extra_env={"CHALK_E2E_EMBED_FUNCTION_NAME": embed_name},
        )

        orchestrate = chalkcompute.RemoteFunction.from_name(orchestrate_name)
        embeddings = orchestrate.remote(["Hello", "world!"])

        assert len(embeddings) == 2, f"Expected 2 embeddings, got {len(embeddings)}"
        for i, emb in enumerate(embeddings):
            assert isinstance(emb, list), f"Embedding {i} is not a list"
            assert len(emb) == 1536, (
                f"Embedding {i} has {len(emb)} dims, expected 1536"
            )
            assert all(isinstance(v, float) for v in emb), (
                f"Embedding {i} has non-float values"
            )
    finally:
        _delete_function(orchestrate_name)
        _delete_function(embed_name)


@needs_staging
@needs_openai
def test_embed_direct_call() -> None:
    """Deploy embed, then call it directly from a local client."""
    function_name = build_scoped_name("ex-embed-direct")
    try:
        _deploy_script(
            "embedding-function", "embed.py", function_name=function_name
        )

        fn = chalkcompute.RemoteFunction.from_name(function_name)
        embedding = fn.remote("hello")

        assert isinstance(embedding, list), (
            f"Expected list, got {type(embedding).__name__}"
        )
        assert len(embedding) == 1536, f"Got {len(embedding)} dims, expected 1536"
        assert all(isinstance(v, float) for v in embedding), (
            "Non-float values in embedding"
        )
    finally:
        _delete_function(function_name)


@needs_staging
@needs_openai
def test_async_embed_direct_call() -> None:
    """Deploy async_embed, then call it with await."""
    function_name = build_scoped_name("ex-async-embed")
    try:
        _deploy_script(
            "embedding-function", "embed_async.py", function_name=function_name
        )

        async def _run() -> list[float]:
            fn = chalkcompute.RemoteFunction.from_name(function_name)
            return await fn.remote("hello")

        embedding = asyncio.run(_run())

        assert isinstance(embedding, list), (
            f"Expected list, got {type(embedding).__name__}"
        )
        assert len(embedding) == 1536, f"Got {len(embedding)} dims, expected 1536"
        assert all(isinstance(v, float) for v in embedding), (
            "Non-float values in embedding"
        )
    finally:
        _delete_function(function_name)
