"""Tests for ``chalkcompute.scorers.exact_match``."""

from __future__ import annotations

import inspect

import pytest

from chalkcompute import scorers
from chalkcompute._evaluation import EvaluationScorerResult, SqlExpression

pytestmark = pytest.mark.usefixtures("no_deploy")

_OUTPUT = """trim(coalesce(CAST("output" AS VARCHAR), ''))"""
_EXPECTED = """trim(coalesce(CAST("expected" AS VARCHAR), ''))"""


def _score(scorer, output: str | None, expected: str | None) -> EvaluationScorerResult:
    return scorer._func(output=output, expected=expected)


def test_compiles_to_an_expression_by_default() -> None:
    scorer = scorers.exact_match()

    assert isinstance(scorer, SqlExpression)
    assert scorer.name == "exact-match"
    assert scorer.expression == (
        f"CASE WHEN {_OUTPUT} = {_EXPECTED} THEN 1.0 ELSE 0.0 END"
    )
    assert scorers.exact_match(name="answer-match").name == "answer-match"


def test_the_expression_carries_the_normalization_options() -> None:
    assert scorers.exact_match(strip=False).expression == (
        """CASE WHEN coalesce(CAST("output" AS VARCHAR), '') = """
        """coalesce(CAST("expected" AS VARCHAR), '') THEN 1.0 ELSE 0.0 END"""
    )
    assert scorers.exact_match(case_insensitive=True).expression == (
        f"CASE WHEN lower({_OUTPUT}) = lower({_EXPECTED}) THEN 1.0 ELSE 0.0 END"
    )
    collapsing = scorers.exact_match(normalize_whitespace=True)
    assert (
        r"""regexp_replace(trim(coalesce(CAST("output" AS VARCHAR), '')), '\s+', ' ')"""
        in (collapsing.expression)
    )
    assert scorers.exact_match(inputs=("answer", "gold")).expression == (
        """CASE WHEN trim(coalesce(CAST("answer" AS VARCHAR), '')) = """
        """trim(coalesce(CAST("gold" AS VARCHAR), '')) THEN 1.0 ELSE 0.0 END"""
    )


def test_rejects_deployment_options_for_an_expression() -> None:
    with pytest.raises(ValueError, match="cannot apply to a SQL scorer"):
        scorers.exact_match(cpu=2)


def test_scores_one_for_the_same_text_and_zero_otherwise() -> None:
    scorer = scorers.exact_match(sql=False)

    assert _score(scorer, "Paris", "Paris") == EvaluationScorerResult(
        score=1.0, metadata={"matched": True}
    )
    assert _score(scorer, "Paris", "Lyon") == EvaluationScorerResult(
        score=0.0, metadata={"matched": False}
    )


def test_deploys_under_a_default_name_with_the_declared_columns() -> None:
    scorer = scorers.exact_match(inputs=("answer", "gold"), sql=False)

    assert scorer._explicit_name == "exact-match"
    assert list(inspect.signature(scorer._func).parameters) == ["answer", "gold"]
    assert (
        scorers.exact_match(name="answer-match", sql=False)._explicit_name
        == "answer-match"
    )


def test_strips_by_default_and_can_fold_case_or_whitespace() -> None:
    assert _score(scorers.exact_match(sql=False), " Paris ", "Paris").score == 1.0
    assert (
        _score(scorers.exact_match(strip=False, sql=False), " Paris ", "Paris").score
        == 0.0
    )
    assert _score(scorers.exact_match(sql=False), "paris", "Paris").score == 0.0
    assert (
        _score(
            scorers.exact_match(case_insensitive=True, sql=False), "paris", "Paris"
        ).score
        == 1.0
    )

    collapsing = scorers.exact_match(normalize_whitespace=True, sql=False)
    assert _score(collapsing, "a  b\n c", "a b c").score == 1.0
    assert _score(scorers.exact_match(sql=False), "a  b\n c", "a b c").score == 0.0


def test_missing_and_empty_columns_do_not_fail_the_row() -> None:
    scorer = scorers.exact_match(sql=False)

    assert _score(scorer, "", "").score == 1.0
    assert _score(scorer, None, None).score == 1.0
    assert _score(scorer, None, "Paris").score == 0.0


def test_rejects_inputs_that_are_not_a_pair() -> None:
    with pytest.raises(ValueError, match="candidate column"):
        scorers.exact_match(inputs=("output",))
    with pytest.raises(ValueError, match="candidate column"):
        scorers.exact_match(inputs=("output", "expected", "extra"))
