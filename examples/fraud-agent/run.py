import json
import os
import subprocess

import chalkcompute


def _get_test_run_hash() -> str:
    """Get a short hash for this test run, only in CI."""
    if not os.environ.get("CI") and not os.environ.get("BUILDKITE"):
        return ""
    if hash_env := os.environ.get("CHALK_TEST_RUN_HASH"):
        return hash_env[:8]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()[:8]
    except Exception:
        pass
    return ""


_HASH = _get_test_run_hash()
_FUNCTION_NAME = f"chalk-sandbox-test-{_HASH}-fraud-agent" if _HASH else "chalk-sandbox-test-fraud-agent"

agent = chalkcompute.RemoteFunction.from_name(name=_FUNCTION_NAME)
agent.wait_ready()

transaction_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
verdict_json = agent(transaction_id)
print(json.dumps(json.loads(verdict_json), indent=2))
