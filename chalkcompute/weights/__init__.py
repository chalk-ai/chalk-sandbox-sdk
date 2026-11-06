"""Publish versioned full model weights and lossless absolute patches.

Install ``chalkcompute[weights]`` in the trainer. Serving replicas use the
``chalkcompute[serving]`` extra and :mod:`chalkcompute.weights.serving`.
"""

from chalkcompute._weights import WeightPublisher as WeightPublisher
from chalkcompute._weights import WeightStream as WeightStream
from chalkcompute._weights_format import WeightStreamError as WeightStreamError

__all__ = ["WeightPublisher", "WeightStream", "WeightStreamError"]
