"""End-to-end test for the embedding-function example.

Deploys embed and orchestrate functions, then verifies the orchestrate
function can call embed and return embeddings.
"""

import os
import subprocess
import sys
import uuid

import pytest
from dotenv import load_dotenv

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")
CHALK_ENVIRONMENT_ID = os.environ.get("CHALK_ENVIRONMENT_ID", "tmnmc9beyujew")

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER or not CHALK_ENVIRONMENT_ID,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER/CHALK_ENVIRONMENT_ID not set",
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


def _deploy_script(script_name: str, *, extra_env: dict[str, str] | None = None) -> None:
    """Run a deploy script as a subprocess."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, os.path.join(EXAMPLE_DIR, script_name)],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    if result.returncode != 0:
        pytest.fail(
            f"{script_name} deploy failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout[-500:]}\n"
            f"stderr: {result.stderr[-500:]}"
        )


def _function_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@needs_staging
def test_embedding_function() -> None:
    """Deploy mock embed + orchestrate, then verify deterministic embeddings."""
    embed_name = _function_name("mock-embed")
    orchestrate_name = _function_name("mock-orchestrate")
    deploy_env = {
        "CHALK_EMBED_FUNCTION_NAME": embed_name,
        "CHALK_ORCHESTRATE_FUNCTION_NAME": orchestrate_name,
    }
    _deploy_script("embed.py", extra_env=deploy_env)
    _deploy_script("orchestrate.py", extra_env=deploy_env)

    import chalkcompute

    orchestrate = chalkcompute.RemoteFunction.from_name(orchestrate_name)
    embeddings = orchestrate(["Hello", "world!"])

    assert len(embeddings) == 2, f"Expected 2 embeddings, got {len(embeddings)}"
    for i, emb in enumerate(embeddings):
        assert isinstance(emb, list), f"Embedding {i} is not a list"
        assert len(emb) == 1536, f"Embedding {i} has {len(emb)} dims, expected 1536"
        assert all(isinstance(v, float) for v in emb), (
            f"Embedding {i} has non-float values"
        )


@needs_staging
@needs_chalkdf_remote
def test_embedding_function_via_chalkdf_remote() -> None:
    """Deploy mock embed, then call it using chalkdf's Remote(...).

    Exercises the same Velox-dispatched direct call path used by
    chalk-private/dataframe/examples/call_embed.py, rather than the
    chalkcompute.RemoteFunction shortcut used in the other tests.
    """
    embed_name = _function_name("mock-embed")
    _deploy_script("embed.py", extra_env={"CHALK_EMBED_FUNCTION_NAME": embed_name})

    from chalk.client.client_grpc import ChalkGRPCClient
    from chalkdf.remote import Remote

    client = ChalkGRPCClient(environment=CHALK_ENVIRONMENT_ID)
    remote = Remote(chalk_client=client)
    fn = remote.function(embed_name)
    embedding = fn("hello")

    assert isinstance(embedding, list), f"Expected list, got {type(embedding).__name__}"
    assert len(embedding) == 1536, f"Got {len(embedding)} dims, expected 1536"
    assert all(isinstance(v, float) for v in embedding), "Non-float values in embedding"


@needs_staging
@needs_chalkdf_remote
def test_async_embedding_function_via_chalkdf_remote() -> None:
    """Deploy mock async embed, then call it via chalkdf's Remote with await."""
    async_embed_name = _function_name("mock-async-embed")
    _deploy_script(
        "embed_async.py",
        extra_env={"CHALK_ASYNC_EMBED_FUNCTION_NAME": async_embed_name},
    )

    import asyncio

    from chalk.client.client_grpc import ChalkGRPCClient
    from chalkdf.remote import Remote

    async def _run() -> list[float]:
        client = ChalkGRPCClient(environment=CHALK_ENVIRONMENT_ID)
        remote = Remote(chalk_client=client)
        fn = remote.function(async_embed_name)
        return await fn("hello")

    embedding = asyncio.run(_run())

    assert isinstance(embedding, list), f"Expected list, got {type(embedding).__name__}"
    assert len(embedding) == 1536, f"Got {len(embedding)} dims, expected 1536"
    assert all(isinstance(v, float) for v in embedding), "Non-float values in embedding"
