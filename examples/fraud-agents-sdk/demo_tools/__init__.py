"""Auxiliary CLIs for the demo: --keep equivalent, packet refresh, manual stop.

These are deliberately *not* part of `story.py` so the main narrative reads
end-to-end without conditional branches. Use them when iterating:

    uv run python -m demo_tools.keep case_fraud         # don't stop the container
    uv run python -m demo_tools.refresh case_fraud      # re-upload after editing samples
    uv run python -m demo_tools.stop <container_id>     # stop a kept-alive container
"""
