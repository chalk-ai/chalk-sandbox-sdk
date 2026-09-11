"""Tests for ``chalkcompute.scorers.string_similarity``."""

from __future__ import annotations

import pytest

from chalkcompute import scorers

pytestmark = pytest.mark.usefixtures("no_deploy")


def _score(scorer, output, expected):
    return scorer._func(output=output, expected=expected)


# Measured against the chalk.functions of the same names, which these mirror.
@pytest.mark.parametrize(
    ("method", "ratio"),
    [
        ("jaccard", 0.4286),
        ("jaro_winkler", 0.7460),
        ("token_set", 0.6154),
        ("token_sort", 0.6154),
        ("partial", 0.6667),
        ("sequence", 0.6154),
    ],
)
def test_each_method_matches_the_chalk_function(method, ratio) -> None:
    graded = _score(scorers.string_similarity(method), "kitten", "sitting")

    assert graded.score == pytest.approx(ratio, abs=1e-4)
    assert graded.metadata["method"] == method
    assert graded.metadata["ratio"] == pytest.approx(ratio, abs=1e-4)


def test_token_methods_ignore_word_order_where_sequence_does_not() -> None:
    reordered = ("hello world", "world hello")

    assert _score(scorers.string_similarity("token_sort"), *reordered).score == 1.0
    assert _score(scorers.string_similarity("sequence"), *reordered).score < 0.5


def test_identical_and_empty_columns_score_one() -> None:
    for method in scorers._SIMILARITY_METHODS:
        scorer = scorers.string_similarity(method)
        assert _score(scorer, "Paris", "Paris").score == pytest.approx(1.0)
        assert _score(scorer, "", "").score == 1.0
        assert _score(scorer, None, None).score == 1.0


def test_case_folding_is_opt_in() -> None:
    folding = scorers.string_similarity("jaccard", case_insensitive=True)

    assert _score(folding, "PARIS", "paris").score == 1.0
    assert _score(scorers.string_similarity("jaccard"), "PARIS", "paris").score < 1.0


def test_names_itself_after_the_method() -> None:
    assert scorers.string_similarity("jaro_winkler")._explicit_name == "jaro-winkler"
    assert (
        scorers.string_similarity("jaccard", name="overlap")._explicit_name == "overlap"
    )


def test_the_image_carries_rapidfuzz() -> None:
    scorer = scorers.string_similarity("token_set")

    installed = [
        package
        for step in scorer._image._steps
        if step.step.field == "pip_install"
        for package in step.step.value.packages
    ]
    assert installed == ["rapidfuzz"]


def test_rejects_an_unknown_method() -> None:
    with pytest.raises(ValueError, match="method must be one of"):
        scorers.string_similarity("cosine")
