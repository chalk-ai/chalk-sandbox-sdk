"""Tests for ``chalkcompute.scorers.contains``."""

from __future__ import annotations

import pytest

from chalkcompute import scorers

pytestmark = pytest.mark.usefixtures("no_deploy")


def _score(scorer, output, expected):
    return scorer._func(output=output, expected=expected)


def test_finds_a_single_substring() -> None:
    scorer = scorers.contains()

    assert _score(scorer, "the capital is Paris", "Paris").score == 1.0
    assert _score(scorer, "the capital is Paris", "Lyon").score == 0.0


def test_a_json_array_asks_for_several_substrings() -> None:
    scorer = scorers.contains()

    graded = _score(scorer, "Paris and Lyon", '["Paris", "Lyon"]')

    assert graded.score == 1.0
    assert graded.metadata == {"matched": ["Paris", "Lyon"], "missing": []}
    assert _score(scorer, "Paris only", '["Paris", "Lyon"]').score == 0.0


def test_modes_score_a_partial_hit_differently() -> None:
    expected = '["Paris", "Lyon", "Nice"]'

    assert _score(scorers.contains(mode="all"), "Paris and Lyon", expected).score == 0.0
    assert _score(scorers.contains(mode="any"), "Paris and Lyon", expected).score == 1.0
    assert _score(
        scorers.contains(mode="fraction"), "Paris and Lyon", expected
    ).score == pytest.approx(2 / 3)


def test_records_what_was_missing() -> None:
    graded = _score(scorers.contains(mode="fraction"), "Paris", '["Paris", "Lyon"]')

    assert graded.metadata == {"matched": ["Paris"], "missing": ["Lyon"]}


def test_case_folding_is_opt_in() -> None:
    assert _score(scorers.contains(), "PARIS", "Paris").score == 0.0
    assert (
        _score(scorers.contains(case_insensitive=True), "PARIS", "Paris").score == 1.0
    )


def test_an_unparsable_array_is_taken_literally() -> None:
    scorer = scorers.contains()

    assert _score(scorer, 'it said ["Paris" here', '["Paris"').score == 1.0


def test_asking_for_nothing() -> None:
    assert _score(scorers.contains(mode="all"), "anything", "").score == 1.0
    assert _score(scorers.contains(mode="any"), "anything", "").score == 0.0
    assert _score(scorers.contains(mode="all"), "anything", "[]").score == 1.0
    assert _score(scorers.contains(), None, None).score == 1.0


def test_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode must be one of"):
        scorers.contains(mode="most")
