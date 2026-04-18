"""End-to-end test for the embedding-function example.

Deploys embed and orchestrate functions, then verifies the orchestrate
function can call embed and return embeddings.
"""

import os
import subprocess
import sys

import pytest
from dotenv import load_dotenv

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)

needs_openai = pytest.mark.skipif(
    not OPENAI_API_KEY,
    reason="OPENAI_API_KEY not set",
)


def _chalkdf_remote_available() -> bool:
    try:
        import chalkdf.remote  # noqa: F401
    except ImportError:
        return False
    return True


needs_chalkdf_remote = pytest.mark.skipif(
    not _chalkdf_remote_available(),
    reason="chalkdf.remote not available (requires a chalkdf release that includes it)",
)

EXAMPLE_DIR = os.path.dirname(__file__)


def _deploy_script(script_name: str) -> None:
    """Run a deploy script as a subprocess."""
    result = subprocess.run(
        [sys.executable, os.path.join(EXAMPLE_DIR, script_name)],
        capture_output=True,
        text=True,
        env=os.environ,
        timeout=600,
    )
    if result.returncode != 0:
        pytest.fail(
            f"{script_name} deploy failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout[-500:]}\n"
            f"stderr: {result.stderr[-500:]}"
        )


@needs_staging
@needs_openai
def test_embedding_function() -> None:
    """Deploy embed + orchestrate, then call orchestrate and verify results."""
    _deploy_script("embed.py")
    _deploy_script("orchestrate.py")

    import chalkcompute

    orchestrate = chalkcompute.RemoteFunction.from_name(name="orchestrate")
    embeddings = orchestrate(["Hello", "world!"])

    assert len(embeddings) == 2, f"Expected 2 embeddings, got {len(embeddings)}"
    for i, emb in enumerate(embeddings):
        assert isinstance(emb, list), f"Embedding {i} is not a list"
        assert len(emb) == 1536, f"Embedding {i} has {len(emb)} dims, expected 1536"
        assert all(isinstance(v, float) for v in emb), (
            f"Embedding {i} has non-float values"
        )


@needs_staging
@needs_openai
@needs_chalkdf_remote
def test_embedding_function_via_chalkdf_remote() -> None:
    """Deploy embed, then call it from a local client using chalkdf's Remote(...).

    Exercises the same Velox-dispatched direct call path used by
    chalk-private/dataframe/examples/call_embed.py, rather than the
    chalkcompute.RemoteFunction shortcut used in the other tests.
    """
    _deploy_script("embed.py")

    import asyncio

    from chalk.client.client_grpc import ChalkGRPCClient
    from chalkdf.remote import Remote

    async def _run() -> list[float]:
        client = ChalkGRPCClient()
        remote = Remote(chalk_client=client)
        fn = remote.function("embed")
        return await fn("hello")

    embedding = asyncio.run(_run())

    assert isinstance(embedding, list), f"Expected list, got {type(embedding).__name__}"
    assert len(embedding) == 1536, f"Got {len(embedding)} dims, expected 1536"
    assert all(isinstance(v, float) for v in embedding), "Non-float values in embedding"
