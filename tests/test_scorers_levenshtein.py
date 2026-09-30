"""Tests for ``chalkcompute.scorers.levenshtein``."""

from __future__ import annotations

import pytest

from chalkcompute import scorers

pytestmark = pytest.mark.usefixtures("no_deploy")


def _score(scorer, output, expected):
    return scorer._func(output=output, expected=expected)


@pytest.mark.parametrize(
    ("a", "b", "distance"),
    [
        ("kitten", "sitting", 3),
        ("flaw", "lawn", 2),
        ("Paris", "Paris", 0),
        ("", "abc", 3),
        ("abc", "", 3),
        ("", "", 0),
    ],
)
def test_distance_matches_the_textbook_values(a, b, distance) -> None:
    assert scorers._levenshtein_distance(a, b) == distance


def test_normalizes_the_distance_into_a_score() -> None:
    scorer = scorers.levenshtein()

    graded = _score(scorer, "kitten", "sitting")

    assert graded.metadata == {"distance": 3}
    assert graded.score == pytest.approx(1 - 3 / 7)


def test_identical_and_empty_text_score_one() -> None:
    scorer = scorers.levenshtein()

    assert _score(scorer, "Paris", "Paris").score == 1.0
    assert _score(scorer, "", "").score == 1.0
    assert _score(scorer, None, None).score == 1.0


def test_nothing_in_common_scores_zero() -> None:
    assert _score(scorers.levenshtein(), "abc", "").score == 0.0
    assert _score(scorers.levenshtein(), "aaa", "bbb").score == 0.0


def test_case_folding_is_opt_in() -> None:
    assert _score(scorers.levenshtein(), "paris", "Paris").score < 1.0
    assert (
        _score(scorers.levenshtein(case_insensitive=True), "paris", "Paris").score
        == 1.0
    )


def test_deploys_under_a_default_name() -> None:
    assert scorers.levenshtein()._explicit_name == "levenshtein"


def test_rejects_inputs_that_are_not_a_pair() -> None:
    with pytest.raises(ValueError, match="candidate column"):
        scorers.levenshtein(inputs=("output",))
