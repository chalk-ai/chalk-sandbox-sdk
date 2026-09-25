"""Tests for ``chalkcompute.scorers.jev``, the judge that escalates when unsure."""

from __future__ import annotations

import json

import pytest

from chalkcompute import scorers

pytestmark = pytest.mark.usefixtures("no_deploy")

_LABELS = {"Y": 1.0, "N": 0.0}


def _reply(choice: str, **probabilities: float) -> dict:
    return {
        "choice": choice,
        "rationale": f"said {choice}",
        "probabilities": [
            {"label": label, "probability": p} for label, p in probabilities.items()
        ],
    }


@pytest.fixture
def replies(monkeypatch: pytest.MonkeyPatch):
    """Each model answers from its own reply; records which models were asked."""
    asked: list[str] = []
    by_model = {
        "cheap": _reply("N", Y=0.05, N=0.95),
        "strong": _reply("Y", Y=0.99, N=0.01),
    }

    def fake_complete(spec, messages):
        asked.append(spec.model)
        return spec.response_model(**by_model[spec.model])

    monkeypatch.setattr(scorers, "_complete", fake_complete)
    return asked, by_model


def _jev(**overrides):
    kwargs: dict = dict(
        labels=_LABELS,
        rubric="Y. The answer is right.\nN. It is not.",
        model="cheap",
        fallback_model="strong",
    )
    kwargs.update(overrides)
    return scorers.jev(**kwargs)


def test_a_confident_grade_is_accepted_without_the_fallback(replies) -> None:
    asked, _ = replies

    result = _jev()._func(output="x")

    assert asked == ["cheap"]
    assert result.score == 0.0
    assert result.metadata["choice"] == "N"
    assert result.metadata["confidence"] == pytest.approx(0.95)
    assert result.metadata["escalated"] is False
    assert result.metadata["judge_model"] == "cheap"


def test_an_unsure_grade_is_regraded_by_the_fallback(replies) -> None:
    asked, by_model = replies
    by_model["cheap"] = _reply("N", Y=0.4, N=0.6)

    result = _jev()._func(output="x")

    # The fallback's grade stands, and the confidence that sent it there is kept.
    assert asked == ["cheap", "strong"]
    assert result.score == 1.0
    assert result.metadata["rationale"] == "said Y"
    assert result.metadata["escalated"] is True
    assert result.metadata["judge_model"] == "strong"
    assert result.metadata["primary_confidence"] == pytest.approx(0.6)


def test_the_threshold_is_the_lowest_confidence_accepted(replies) -> None:
    asked, by_model = replies
    by_model["cheap"] = _reply("N", Y=0.1, N=0.9)

    _jev(threshold=0.9)._func(output="x")

    assert asked == ["cheap"]


@pytest.mark.parametrize(
    "reply",
    [
        # No probabilities at all.
        _reply("N"),
        # Picking the label it rated less likely.
        _reply("N", Y=0.7, N=0.3),
        # A choice that is not a label.
        _reply("Maybe", Y=0.5, N=0.5),
    ],
)
def test_a_reply_that_is_not_sure_of_its_choice_escalates(replies, reply) -> None:
    asked, by_model = replies
    by_model["cheap"] = reply

    _jev()._func(output="x")

    assert asked == ["cheap", "strong"]


def test_confidence_is_the_normalized_share_on_the_choice(replies) -> None:
    _, by_model = replies
    # Unnormalized, differently cased, with a label outside the rubric and a
    # negative entry, both ignored.
    by_model["cheap"] = {
        "choice": "n.",
        "rationale": "r",
        "probabilities": [
            {"label": "n", "probability": 3},
            {"label": "y", "probability": 1},
            {"label": "Z", "probability": 5},
            {"label": "Y", "probability": -1},
        ],
    }

    result = _jev(threshold=0.5)._func(output="x")

    assert result.score == 0.0
    assert result.metadata["confidence"] == pytest.approx(0.75)
    assert result.metadata["probabilities"] == {"N": 0.75, "Y": 0.25}


def test_labels_may_score_anywhere_in_the_range(replies) -> None:
    _, by_model = replies
    by_model["cheap"] = _reply("B", A=0.1, B=0.9)

    result = _jev(labels={"A": 0.0, "B": 0.5, "C": 1.0})._func(output="x")

    assert result.score == 0.5


def test_the_prompt_carries_the_rubric_labels_and_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[dict]] = []

    def fake_complete(spec, messages):
        seen.append(messages)
        return spec.response_model(**_reply("Y", Y=1.0, N=0.0))

    monkeypatch.setattr(scorers, "_complete", fake_complete)

    _jev(inputs=("input", "output"))._func(input="2 + 2?", output="4")

    prompt = seen[0][-1]["content"]
    assert "Y. The answer is right." in prompt
    assert "Choose exactly one label: Y, N." in prompt
    assert '"probabilities"' in prompt
    assert "## input\n\n2 + 2?" in prompt and "## output\n\n4" in prompt


def test_declares_a_scorer_with_its_metadata_schema() -> None:
    scorer = _jev(inputs=("input", "output", "expected"))

    assert scorer._explicit_name == "jev"
    assert scorer._func.__globals__["__name__"] == __name__
    schema = json.loads(scorer._func.__chalk_scorer_response_schema__)
    assert {"confidence", "escalated", "judge_model"} <= set(schema["properties"])


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (dict(labels={"Y": 1.0}), "at least two"),
        (dict(labels={"Y": 1.0, "y": 0.0}), "repeat"),
        (dict(labels={"Y": 1.0, "": 0.0}), "non-empty"),
        (dict(labels={"Y": 2.0, "N": 0.0}), r"\[0, 1\]"),
        (dict(labels={"Y": True, "N": 0.0}), r"\[0, 1\]"),
        (dict(threshold=0), r"\(0, 1\]"),
        (dict(threshold=1.5), r"\(0, 1\]"),
        (dict(rubric="  "), "rubric"),
    ],
)
def test_rejects_a_bad_declaration(overrides: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _jev(**overrides)
