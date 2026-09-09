"""Tests for ``chalkcompute.scorers.numeric_close``."""

from __future__ import annotations

import pytest

from chalkcompute import scorers

pytestmark = pytest.mark.usefixtures("no_deploy")


def _score(scorer, output, expected):
    return scorer._func(output=output, expected=expected)


def test_a_tolerance_gives_a_pass_or_a_fail() -> None:
    scorer = scorers.numeric_close(tolerance=0.5)

    assert _score(scorer, "10.2", "10").score == 1.0
    assert _score(scorer, "11", "10").score == 0.0
    assert _score(scorer, "10", "10").metadata == {"difference": 0.0}


def test_a_relative_tolerance_scales_with_the_values() -> None:
    scorer = scorers.numeric_close(tolerance=0.1, relative=True)

    assert _score(scorer, "105", "100").score == 1.0
    assert _score(scorer, "150", "100").score == 0.0
    # The same 5 apart, but now well outside a tenth of the value.
    assert _score(scorer, "6", "1").score == 0.0


def test_without_a_tolerance_the_score_falls_off_with_the_gap() -> None:
    scorer = scorers.numeric_close()

    assert _score(scorer, "10", "10").score == 1.0
    assert _score(scorer, "9", "10").score == pytest.approx(0.9)
    assert _score(scorer, "5", "10").score == pytest.approx(0.5)
    assert _score(scorer, "-10", "10").score == 0.0


def test_two_zeroes_are_a_perfect_score() -> None:
    assert _score(scorers.numeric_close(), "0", "0").score == 1.0
    assert _score(scorers.numeric_close(), "0", "0").metadata == {"difference": 0.0}


def test_records_the_signed_difference() -> None:
    assert _score(scorers.numeric_close(), "8", "10").metadata == {"difference": -2.0}
    assert _score(scorers.numeric_close(), "12", "10").metadata == {"difference": 2.0}


def test_text_that_is_not_a_number_scores_zero() -> None:
    scorer = scorers.numeric_close()

    assert _score(scorer, "about ten", "10") == scorers.EvaluationScorerResult(
        score=0.0, metadata={"error": "not a number"}
    )
    assert _score(scorer, None, "10").score == 0.0
    assert _score(scorer, " 10 ", "10").score == 1.0


def test_rejects_a_negative_tolerance() -> None:
    assert scorers.numeric_close()._explicit_name == "numeric-close"
    with pytest.raises(ValueError, match="tolerance cannot be negative"):
        scorers.numeric_close(tolerance=-1)
