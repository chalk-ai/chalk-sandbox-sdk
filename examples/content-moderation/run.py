"""Start a content moderation workflow for an image.

Usage:
    python run.py <image_url>
    python run.py  # uses a default test image
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from temporalio.client import Client

from workflow import TASK_QUEUE, ModerationWorkflow

TEMPORAL_ADDRESS = "localhost:7233"

# Public-domain cat image from Wikimedia Commons.
DEFAULT_IMAGE = "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4d/Cat_November_2010-1a.jpg/1200px-Cat_November_2010-1a.jpg"


async def main() -> None:
    image_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IMAGE
    workflow_id = f"moderation-{uuid.uuid4().hex[:8]}"

    client = await Client.connect(TEMPORAL_ADDRESS)
    handle = await client.start_workflow(
        ModerationWorkflow.run,
        image_url,
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    print(f"Workflow started: {workflow_id}")
    print(f"Image: {image_url}")

    result = await handle.result()

    print(f"\nClassification: top_label={result.top_label}  confidence={result.top_confidence}")
    if result.decision == "auto_approved":
        print("Decision: auto-approved (model confident)")
    else:
        print(f"Decision: {result.decision}  reviewed_by: {result.reviewed_by}")


if __name__ == "__main__":
    asyncio.run(main())
