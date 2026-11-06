"""Tests for the deployed NDCG scorer contract and ranking semantics."""

from __future__ import annotations

import json
import math

import pytest

from chalkcompute import scorers

pytestmark = pytest.mark.usefixtures("no_deploy")


def _score(output, relevance, k=10):
    return scorers.ndcg(k=k)._func(
        output=json.dumps(output), relevance=json.dumps(relevance)
    )


def test_worked_example_and_metadata():
    result = _score(["b", "d", "a"], {"a": 3, "b": 2, "c": 1, "d": 0}, k=3)
    ideal = 7 + 3 / math.log2(3) + 1 / 2
    assert result.score == pytest.approx(6.5 / ideal)
    assert result.metadata == pytest.approx({"k": 3, "dcg": 6.5, "idcg": ideal})


def test_ideal_ranking_and_equal_relevance_ties():
    relevance = {"a": 3, "b": 2, "c": 2}
    assert _score(["a", "b", "c"], relevance).score == 1.0
    assert _score(["a", "c", "b"], relevance).score == 1.0
    assert _score(["b", "a", "c"], relevance).score < 1.0


def test_cutoff_applies_to_both_rankings():
    relevance = {"a": 3, "b": 1}
    assert _score(["a", "b"], relevance, k=1).score == 1.0
    assert _score(["b", "a"], relevance, k=1).score == pytest.approx(1 / 7)


def test_missing_reference_items_still_contribute_to_ideal():
    assert _score(["b"], {"a": 3, "b": 1}, k=1).score == pytest.approx(1 / 7)
    assert _score(["a"], {"a": 1, "b": 1}, k=2).score == pytest.approx(
        1 / (1 + 1 / math.log2(3))
    )


def test_unknown_items_keep_their_positions_but_earn_no_gain():
    assert _score(["unknown", "a"], {"a": 1}).score == pytest.approx(1 / math.log2(3))


@pytest.mark.parametrize(
    "output,relevance",
    [([], {"a": 1}), (["a"], {}), ([], {}), (["a"], {"a": 0}), (["x"], {"a": 1})],
)
def test_zero_gain(output, relevance):
    result = _score(output, relevance)
    assert result.score == 0.0
    assert "error" not in result.metadata


def test_fractional_and_very_small_grades():
    assert _score(["a"], {"a": 0.5}).score == 1.0
    assert _score(["a"], {"a": 1e-20}).score == 1.0


@pytest.mark.parametrize("k", [0, -1, True, 1.5, "10", None])
def test_invalid_cutoff(k):
    with pytest.raises(ValueError, match="positive integer"):
        scorers.ndcg(k=k)


@pytest.mark.parametrize(
    "output,relevance",
    [
        ("not json", "{}"),
        ("[]", "not json"),
        (None, "{}"),
        ("{}", "{}"),
        ("[1]", "{}"),
        ("[null]", "{}"),
        ('[["a"]]', "{}"),
        ('["a", "a"]', '{"a": 1}'),
        ("[]", "[]"),
        ("[]", "null"),
        ('["a"]', '{"a": -1}'),
        ('["a"]', '{"a": true}'),
        ('["a"]', '{"a": "3"}'),
        ('["a"]', '{"a": null}'),
        ('["a"]', '{"a": NaN}'),
        ('["a"]', '{"a": Infinity}'),
        ('["a"]', '{"a": 1e999}'),
        ('["a"]', '{"a": 1025}'),
        ('["a", "b"]', '{"a": 1023, "b": 1023, "c": 1023}'),
    ],
)
def test_invalid_rows_score_zero_with_error(output, relevance):
    result = scorers.ndcg()._func(output=output, relevance=relevance)
    assert result.score == 0.0
    assert result.metadata["error"]


def test_declaration_and_custom_column_bindings():
    assert scorers.ndcg()._explicit_name == "ndcg"
    scorer = scorers.ndcg(inputs=("ranked", "labels"), name="supplier-ndcg", k=1)
    assert scorer._explicit_name == "supplier-ndcg"
    assert scorer._func(ranked='["a"]', labels='{"a": 1}').score == 1.0
    with pytest.raises(ValueError, match="inputs must name"):
        scorers.ndcg(inputs=("output",))


def test_rejects_strip_mode(monkeypatch):
    monkeypatch.setattr(scorers, "should_strip_source", lambda: True)
    with pytest.raises(scorers.FunctionBuildError, match="ndcg"):
        scorers.ndcg()
