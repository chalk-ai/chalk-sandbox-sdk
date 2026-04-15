"""Temporal workflow for content moderation with human-in-the-loop.

The workflow:
1. Calls the Chalk classify_image function (runs in an isolated container)
2. If confidence >= threshold: auto-approve
3. If confidence < threshold: pause and wait for human review via signal
4. Returns the final moderation decision
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow

CONFIDENCE_THRESHOLD = 0.85
REVIEW_TIMEOUT = timedelta(hours=24)
TASK_QUEUE = "content-moderation"


@dataclass
class ModerationResult:
    top_label: str
    top_confidence: float
    needs_review: bool
    decision: str
    reviewed_by: str


@activity.defn
def classify_image_activity(image_url: str) -> dict:
    """Call the Chalk classify_image function from a Temporal activity.

    The actual ML inference runs inside a Chalk container — this activity
    is just the bridge between Temporal and Chalk.
    """
    import json

    import chalkcompute

    classify = chalkcompute.RemoteFunction.from_name("classify-image")
    return json.loads(classify(image_url))


@workflow.defn
class ModerationWorkflow:
    """Classify an image and optionally wait for human review."""

    def __init__(self) -> None:
        self._decision: str | None = None

    @workflow.run
    async def run(self, image_url: str) -> ModerationResult:
        # Step 1: classify inside a Chalk container
        result = await workflow.execute_activity(
            classify_image_activity,
            image_url,
            start_to_close_timeout=timedelta(minutes=5),
        )

        top_label = result["top_label"]
        top_confidence = result["top_confidence"]
        needs_review = result["needs_review"]

        # Step 2: auto-approve if the model is confident
        if not needs_review:
            return ModerationResult(
                top_label=top_label, top_confidence=top_confidence,
                needs_review=False, decision="auto_approved", reviewed_by="model",
            )

        # Step 3: wait for a human reviewer to signal a decision
        workflow.logger.info(
            "Waiting for human review — top_label=%s confidence=%.2f",
            top_label, top_confidence,
        )
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=REVIEW_TIMEOUT,
            )
            decision = self._decision
            reviewed_by = "human"
        except asyncio.TimeoutError:
            decision = "auto_rejected"
            reviewed_by = "timeout"

        return ModerationResult(
            top_label=top_label, top_confidence=top_confidence,
            needs_review=True, decision=decision, reviewed_by=reviewed_by,
        )

    @workflow.signal
    def review(self, decision: str) -> None:
        """Signal from a human reviewer: 'approved' or 'rejected'."""
        self._decision = decision
