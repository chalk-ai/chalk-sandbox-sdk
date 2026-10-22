"""Platform-managed OIDC token files for sandboxes.

Requires a Chalk deployment that supports ``oidc_token_files`` (META-769).
"""

from __future__ import annotations

import json

import pytest
from chalkcompute import (
    ChalkIdentity,
    OIDCTokenFile,
    Sandbox,
    SandboxClient,
    SandboxCreationError,
)
from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import needs_staging

AUDIENCE = "oidc-token-file-test.example.com"

# Print only the verified-shape claims. The raw token stays inside the sandbox, so
# it never reaches the SDK process.
_DESCRIBE_TOKEN = """
import base64, json, os, sys
with open(os.environ["TEST_OIDC_TOKEN_FILE"]) as f:
    token = f.read().strip()
payload = token.split(".")[1]
payload += "=" * (-len(payload) % 4)
claims = json.loads(base64.urlsafe_b64decode(payload))
print(json.dumps({"aud": claims["aud"], "sub": claims["sub"], "exp": claims["exp"]}))
"""


@needs_staging
def test_sandbox_reads_platform_managed_oidc_token_file():
    with SandboxClient.from_env() as client:
        sandbox = Sandbox(
            name=build_scoped_name("sandbox-oidc"),
            lifetime="600s",
            chalk_identity=ChalkIdentity(mode="sandbox"),
            env={"TEST_OIDC_TOKEN_FILE": OIDCTokenFile(audience=AUDIENCE)},
            sandbox_client=client,
        )
        try:
            try:
                sandbox.run(timeout=300)
            except SandboxCreationError as error:
                if "does not support OIDCTokenFile" in str(error):
                    pytest.skip("staging does not yet support OIDC token files")
                raise
            assert sandbox.id is not None

            # The first token exists before user code starts.
            result = sandbox.exec("python3", "-c", _DESCRIBE_TOKEN)
            assert result.exit_code == 0, result.stderr_text
            claims = json.loads(result.stdout_text)
            assert claims["aud"] in (AUDIENCE, [AUDIENCE])
            # The subject identifies the sandbox, not the user who created it.
            assert claims["sub"].endswith(f":sandbox:{sandbox.id}")

            # The platform owns the file: the path comes from the env var, the
            # directory is read-only to the workload, and the file is private.
            path = sandbox.exec(
                "sh", "-c", 'printf %s "$TEST_OIDC_TOKEN_FILE"'
            ).stdout_text
            assert path.startswith("/")
            assert sandbox.exec("test", "-r", path).exit_code == 0
            assert sandbox.exec("sh", "-c", f"echo x >> {path}").exit_code != 0
        finally:
            if sandbox.id is not None:
                sandbox.terminate()
