"""Tests for escalating an unsure ``llm_judge`` verdict to a stronger model."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from chalkcompute import scorers

pytestmark = pytest.mark.usefixtures("no_deploy")


class Verdict(BaseModel):
    score: float = Field(ge=0, le=1)
    confidence: float
    reason: str


@pytest.fixture
def replies(monkeypatch: pytest.MonkeyPatch):
    """Each model answers from its own reply; records which models were asked."""
    asked: list[str] = []
    by_model: dict[str, dict] = {
        "cheap": {"score": 0.0, "confidence": 0.95, "reason": "cheap"},
        "strong": {"score": 1.0, "confidence": 0.99, "reason": "strong"},
    }

    def fake_complete(spec, messages):
        asked.append(spec.model)
        return dict(by_model[spec.model])

    monkeypatch.setattr(scorers, "_complete", fake_complete)
    return asked, by_model


def _cascade(**overrides):
    kwargs: dict = dict(model="cheap", fallback_model="strong")
    kwargs.update(overrides)
    return scorers.llm_judge(Verdict, **kwargs)


def test_a_confident_verdict_is_accepted_without_the_fallback(replies) -> None:
    asked, _ = replies

    result = _cascade()._func(output="x")

    assert asked == ["cheap"]
    assert result.score == 0.0
    assert result.metadata["judge_model"] == "cheap"
    assert result.metadata["escalated"] is False
    assert result.metadata["primary_confidence"] == 0.95


def test_an_unsure_verdict_is_regraded_by_the_fallback(replies) -> None:
    asked, by_model = replies
    by_model["cheap"]["confidence"] = 0.6

    result = _cascade()._func(output="x")

    # The fallback's verdict wins, and the confidence that sent it there is kept.
    assert asked == ["cheap", "strong"]
    assert result.score == 1.0
    assert result.metadata["reason"] == "strong"
    assert result.metadata["judge_model"] == "strong"
    assert result.metadata["escalated"] is True
    assert result.metadata["primary_confidence"] == 0.6


def test_the_threshold_is_a_lower_bound_on_what_is_accepted(replies) -> None:
    asked, by_model = replies
    by_model["cheap"]["confidence"] = 0.8

    _cascade(confidence_threshold=0.8)._func(output="x")

    assert asked == ["cheap"]


@pytest.mark.parametrize("unusable", [1.5, -0.1])
def test_a_confidence_outside_the_range_escalates(replies, unusable) -> None:
    # Fail toward the stronger judge rather than trusting a malformed number.
    asked, by_model = replies
    by_model["cheap"]["confidence"] = unusable

    _cascade()._func(output="x")

    assert asked == ["cheap", "strong"]


def test_without_a_fallback_nothing_escalates_or_is_added(replies) -> None:
    asked, by_model = replies
    by_model["cheap"]["confidence"] = 0.1

    result = scorers.llm_judge(Verdict, model="cheap")._func(output="x")

    assert asked == ["cheap"]
    assert set(result.metadata) == {"confidence", "reason"}


def test_a_fallback_needs_a_confidence_field() -> None:
    class NoConfidence(BaseModel):
        score: float

    with pytest.raises(ValueError, match="confidence"):
        scorers.llm_judge(NoConfidence, model="cheap", fallback_model="strong")


@pytest.mark.parametrize("threshold", [0, -0.5, 1.5])
def test_the_threshold_must_be_a_probability(threshold) -> None:
    with pytest.raises(ValueError, match="confidence_threshold"):
        _cascade(confidence_threshold=threshold)
