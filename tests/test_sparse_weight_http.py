from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "sparse_weight_http_check",
    Path(__file__).parents[1] / "scripts/check_sparse_weight_http.py",
)
assert spec is not None and spec.loader is not None
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def response():
    return {
        "choices": [
            {
                "text": "hello",
                "logprobs": {
                    "tokens": ["hello"],
                    "top_logprobs": [
                        {"hello": -0.1, "a": -1.0, "b": -2.0, "c": -3.0, "d": -4.0}
                    ],
                },
            }
        ]
    }


def test_oracle_preserves_fixed_threshold():
    actual, reference = response(), response()
    actual["choices"][0]["logprobs"]["top_logprobs"][0]["hello"] += 0.0009
    assert checker.compare(actual, reference)["maximum_logprob_error"] < 1e-3
    actual["choices"][0]["logprobs"]["top_logprobs"][0]["hello"] += 0.0002
    with pytest.raises(ValueError, match="1e-3"):
        checker.compare(actual, reference)


@pytest.mark.parametrize("change", ["text", "tokens", "membership", "missing", "nan"])
def test_oracle_refuses_incomplete_or_different_results(change):
    actual = copy.deepcopy(response())
    choice = actual["choices"][0]
    if change == "text":
        choice["text"] = "different"
    elif change == "tokens":
        choice["logprobs"]["tokens"] = ["different"]
    elif change == "membership":
        choice["logprobs"]["top_logprobs"][0]["other"] = choice["logprobs"][
            "top_logprobs"
        ][0].pop("a")
    elif change == "missing":
        choice["logprobs"]["top_logprobs"] = []
    else:
        choice["logprobs"]["top_logprobs"][0]["a"] = float("nan")
    with pytest.raises(ValueError):
        checker.compare(actual, response())
