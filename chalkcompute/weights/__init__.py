"""Publish versioned full model weights and lossless absolute patches.

Install ``chalkcompute[weights]`` in the trainer. Serving replicas use the
``chalkcompute[serving]`` extra and :mod:`chalkcompute.weights.serving`.
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from chalkcompute._weights import WeightPublisher as WeightPublisher
    from chalkcompute._weights import WeightStream as WeightStream
    from chalkcompute._weights_format import WeightStreamError as WeightStreamError
    from chalkcompute.weights.layout import (
        Concatenate as Concatenate,
        Interleave as Interleave,
        Reshape as Reshape,
        Split as Split,
        TensorMapping as TensorMapping,
        TensorSpec as TensorSpec,
        Transpose as Transpose,
        WeightLayout as WeightLayout,
        WeightSchema as WeightSchema,
    )

_LAYOUT_EXPORTS = {
    "WeightLayout",
    "WeightSchema",
    "TensorSpec",
    "TensorMapping",
    "Concatenate",
    "Split",
    "Transpose",
    "Reshape",
    "Interleave",
}

__all__ = [
    "WeightPublisher",
    "WeightStream",
    "WeightStreamError",
    *sorted(_LAYOUT_EXPORTS),
]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    if name in _LAYOUT_EXPORTS:
        value = getattr(importlib.import_module("chalkcompute.weights.layout"), name)
        globals()[name] = value
        return value
    module = (
        "chalkcompute._weights_format"
        if name == "WeightStreamError"
        else "chalkcompute._weights"
    )
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value
