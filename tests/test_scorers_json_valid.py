"""Tests for ``chalkcompute.scorers.json_valid``."""

from __future__ import annotations

import pytest

from chalkcompute import scorers

pytestmark = pytest.mark.usefixtures("no_deploy")


@pytest.mark.parametrize(
    "text", ['{"a": 1}', "[1, 2, 3]", '"a string"', "17", "null", "true"]
)
def test_anything_json_scores_one(text) -> None:
    graded = scorers.json_valid()._func(output=text)

    assert graded.score == 1.0
    assert graded.metadata == {}


# Python's parser takes these, but the JSON grammar has no place for them.
@pytest.mark.parametrize(
    "text", ["NaN", "Infinity", "-Infinity", '{"x": Infinity}', "[NaN]"]
)
def test_the_nonstandard_constants_are_not_json(text) -> None:
    graded = scorers.json_valid()._func(output=text)

    assert graded.score == 0.0
    assert "not valid JSON" in graded.metadata["error"]


@pytest.mark.parametrize("text", ["{not json", "", "{'a': 1}", "undefined"])
def test_anything_else_scores_zero_and_says_why(text) -> None:
    graded = scorers.json_valid()._func(output=text)

    assert graded.score == 0.0
    assert graded.metadata["error"]


def test_a_missing_column_does_not_fail_the_row() -> None:
    assert scorers.json_valid()._func(output=None).score == 0.0


def test_reads_exactly_one_column() -> None:
    assert scorers.json_valid()._explicit_name == "json-valid"
    assert scorers.json_valid(inputs=("payload",))._func(payload="{}").score == 1.0
    with pytest.raises(ValueError, match="exactly one column"):
        scorers.json_valid(inputs=("output", "expected"))
