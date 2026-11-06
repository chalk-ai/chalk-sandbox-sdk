"""Publish versioned full model weights and lossless absolute patches.

Install ``chalkcompute[weights]`` in the trainer. Serving replicas use the
``chalkcompute[serving]`` extra and :mod:`chalkcompute.weights.serving`.
"""

import importlib
from typing import TYPE_CHECKING

from chalkcompute._weights_format import WeightStreamError as WeightStreamError

if TYPE_CHECKING:
    from chalkcompute._weights import WeightPublisher as WeightPublisher
    from chalkcompute._weights import WeightStream as WeightStream


def __getattr__(name: str) -> object:
    # Serving worker/codec imports do not need the Volume/native client stack.
    if name in {"WeightPublisher", "WeightStream"}:
        return getattr(importlib.import_module("chalkcompute._weights"), name)
    raise AttributeError(f"module 'chalkcompute.weights' has no attribute {name}")


__all__ = ["WeightPublisher", "WeightStream", "WeightStreamError"]
