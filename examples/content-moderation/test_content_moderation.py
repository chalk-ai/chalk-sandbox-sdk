"""End-to-end test for the content moderation classify function.

Deploys the classify_image function to Chalk and verifies it can
classify images from URLs inside an isolated container.
"""

import os
import subprocess
import sys

import pytest
from dotenv import load_dotenv

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)

EXAMPLE_DIR = os.path.dirname(__file__)

# Public-domain cat image from Wikimedia Commons.
TEST_IMAGE_URL = "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4d/Cat_November_2010-1a.jpg/1200px-Cat_November_2010-1a.jpg"


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
def test_classify_returns_predictions() -> None:
    """Deploy classify_image and verify it returns labeled predictions."""
    import json

    _deploy_script("classify.py")

    import chalkcompute

    classify = chalkcompute.RemoteFunction.from_name("classify-image")
    result = json.loads(classify(TEST_IMAGE_URL))

    assert len(result["labels"]) > 0
    assert isinstance(result["top_label"], str)
    assert isinstance(result["top_confidence"], float)
    assert 0.0 <= result["top_confidence"] <= 1.0
    assert isinstance(result["needs_review"], bool)


@needs_staging
def test_classify_identifies_known_image() -> None:
    """The test image is a cat — the model should classify it as such."""
    import json

    import chalkcompute

    classify = chalkcompute.RemoteFunction.from_name("classify-image")
    result = json.loads(classify(TEST_IMAGE_URL))

    all_labels = [lbl["label"].lower() for lbl in result["labels"]]
    cat_labels = [name for name in all_labels if "cat" in name or "tabby" in name or "egyptian" in name]
    assert len(cat_labels) > 0, f"Expected cat-related label, got: {all_labels}"
