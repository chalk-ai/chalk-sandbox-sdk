"""Tests for ``chalkcompute.scorers.exact_match``."""

from __future__ import annotations

import inspect

import pytest

from chalkcompute import ScaleFromZeroRequestPolicy, scorers
from chalkcompute._evaluation import EvaluationScorerResult

pytestmark = pytest.mark.usefixtures("no_deploy")


def _score(scorer, output: str | None, expected: str | None) -> EvaluationScorerResult:
    return scorer._func(output=output, expected=expected)


def test_scores_one_for_the_same_text_and_zero_otherwise() -> None:
    scorer = scorers.exact_match()

    assert _score(scorer, "Paris", "Paris") == EvaluationScorerResult(
        score=1.0, metadata={"matched": True}
    )
    assert _score(scorer, "Paris", "Lyon") == EvaluationScorerResult(
        score=0.0, metadata={"matched": False}
    )


def test_deploys_under_a_default_name_with_the_declared_columns() -> None:
    scorer = scorers.exact_match(inputs=("answer", "gold"))

    assert scorer._explicit_name == "exact-match"
    assert list(inspect.signature(scorer._func).parameters) == ["answer", "gold"]
    assert scorers.exact_match(name="answer-match")._explicit_name == "answer-match"


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, (0, 1, 1)),
        ({"target_queue_depth": 4}, (0, 1, 4)),
        ({"min_replicas": 1}, (1, 1, None)),
        ({"max_replicas": 1}, (1, 1, None)),
        ({"max_replicas": 3}, (1, 3, None)),
        ({"min_replicas": 0, "max_replicas": 4}, (0, 4, None)),
        ({"gpu": "nvidia-t4:1", "target_gpu_utilization_percentage": 75}, (1, 1, None)),
    ],
)
def test_scorer_replica_defaults(options, expected) -> None:
    scorer = scorers.exact_match(**options)

    assert (
        scorer._min_replicas,
        scorer._max_replicas,
        scorer._target_queue_depth,
    ) == expected


def test_scorer_with_hold_still_requests_async_queue_activation() -> None:
    # HOLD serves synchronous traffic. The platform rejects HOLD plus a queue
    # trigger, so an incompatible override cannot silently strand an Eval call.
    scorer = scorers.exact_match(
        scale_from_zero_request_policy=ScaleFromZeroRequestPolicy.HOLD
    )

    assert scorer._scale_from_zero_request_policy is ScaleFromZeroRequestPolicy.HOLD
    assert scorer._target_queue_depth == 1


def test_strips_by_default_and_can_fold_case_or_whitespace() -> None:
    assert _score(scorers.exact_match(), " Paris ", "Paris").score == 1.0
    assert _score(scorers.exact_match(strip=False), " Paris ", "Paris").score == 0.0
    assert _score(scorers.exact_match(), "paris", "Paris").score == 0.0
    assert (
        _score(scorers.exact_match(case_insensitive=True), "paris", "Paris").score
        == 1.0
    )

    collapsing = scorers.exact_match(normalize_whitespace=True)
    assert _score(collapsing, "a  b\n c", "a b c").score == 1.0
    assert _score(scorers.exact_match(), "a  b\n c", "a b c").score == 0.0


def test_missing_and_empty_columns_do_not_fail_the_row() -> None:
    scorer = scorers.exact_match()

    assert _score(scorer, "", "").score == 1.0
    assert _score(scorer, None, None).score == 1.0
    assert _score(scorer, None, "Paris").score == 0.0


def test_rejects_inputs_that_are_not_a_pair() -> None:
    with pytest.raises(ValueError, match="candidate column"):
        scorers.exact_match(inputs=("output",))
    with pytest.raises(ValueError, match="candidate column"):
        scorers.exact_match(inputs=("output", "expected", "extra"))
