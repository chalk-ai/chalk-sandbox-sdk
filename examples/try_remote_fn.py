"""Smoke test: call an already-deployed remote function.

Deploy the target first (defines the `ex-add` function):
    uv run python examples/scalar/deploy_sync.py

Then call it:
    uv run python examples/try_remote_fn.py

Auth: if a `.env` file is present (e.g. `sops -d .env.enc > .env` for ftqa),
its CHALK_* vars are loaded and take precedence. Otherwise it falls back to the
~/.chalk.yml `default` token (http://localhost:4002, env avi-chalk-ai) — in
which case make sure the local API server is up on :4002.
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv

import chalkcompute

# Load .env (ftqa creds decrypted from .env.enc) before chalkcompute reads auth.
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

FUNCTION_NAME = "ex-add"  # deployed by examples/scalar/deploy_sync.py


def _print_backend() -> None:
    """Resolve and print which API server / environment we connected to."""
    client = chalkcompute.ConnectClient()
    client._ensure_auth()
    print(f"connected to api_server={client._api_server!r} env_id={client._env_id!r}")


def main() -> None:
    _print_backend()

    # Attach to the already-deployed function by name (no deploy here).
    fn = chalkcompute.RemoteFunction.from_name(FUNCTION_NAME)

    # --- CALL (blocking) --------------------------------------------------
    print("remote(1, 10) =", fn.remote(1, 10))

    # --- CALL (deferred) --------------------------------------------------
    handle = fn.defer(2, 20)
    print("defer(2, 20).get() =", handle.get(timeout=60))


if __name__ == "__main__":
    main()
