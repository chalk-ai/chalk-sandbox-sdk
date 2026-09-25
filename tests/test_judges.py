"""Tests for the prebuilt judges other than ``factuality``."""

from __future__ import annotations

import json

import pytest

from chalkcompute import scorers

pytestmark = pytest.mark.usefixtures("no_deploy")

_JUDGES = [
    "battle",
    "closed_qa",
    "factuality",
    "humor",
    "moderation",
    "possible",
    "security",
    "sql_match",
    "summary",
    "translation",
]


def _spec(scorer):
    return scorer._func.__globals__["__chalk_spec__"]


def _graded(scorer, choice: str, probabilities: dict | None = None) -> dict:
    entries = [
        {"letter": letter, "probability": probability}
        for letter, probability in (probabilities or {}).items()
    ]
    model = _spec(scorer).response_model
    return scorers._dump(model(choice=choice, rationale="r", probabilities=entries))


def _score(scorer, choice: str) -> float:
    return _graded(scorer, choice)["score"]


@pytest.mark.parametrize("judge", _JUDGES)
def test_every_judge_declares_a_scorer_in_the_callers_module(judge: str) -> None:
    scorer = getattr(scorers, judge)(model="gpt-4o-mini")

    assert _spec(scorer).model == "gpt-4o-mini"
    # The pod re-imports the declaring module and reruns the call that made it.
    assert scorer._func.__globals__["__name__"] == __name__
    assert set(_spec(scorer).inputs) <= {
        "input",
        "output",
        "expected",
        "criteria",
    }


@pytest.mark.parametrize("judge", _JUDGES)
def test_every_judge_keeps_the_choice_and_reasoning_as_metadata(judge: str) -> None:
    scorer = getattr(scorers, judge)(model="m")

    schema = json.loads(scorer._func.__chalk_scorer_response_schema__)
    assert sorted(schema["properties"]) == [
        "choice",
        "confidence",
        "probabilities",
        "rationale",
    ]


@pytest.mark.parametrize("judge", _JUDGES)
def test_every_judge_scores_an_unusable_reply_as_zero(judge: str) -> None:
    scorer = getattr(scorers, judge)(model="m")

    # "Neither" must not read as the letter N, which on a safety judge would
    # score a row clean on a reply the model never meant that way.
    for unusable in ("nonsense", "Neither", "none of these", ""):
        assert _score(scorer, unusable) == 0.0


@pytest.mark.parametrize("judge", _JUDGES)
def test_punctuation_around_the_letter_still_grades(judge: str) -> None:
    scorer = getattr(scorers, judge)(model="m")
    letters = {"battle": "1", "summary": "1", "translation": "5", "humor": "5"}
    letter = letters.get(judge, "Y" if judge not in {"factuality"} else "C")

    assert _score(scorer, f" {letter.lower()}. ") == _score(scorer, letter)


@pytest.mark.parametrize(
    ("judge", "better", "worse"),
    [("battle", "1", "2"), ("summary", "1", "2")],
)
def test_a_head_to_head_judge_prefers_the_output(
    judge: str, better: str, worse: str
) -> None:
    scorer = getattr(scorers, judge)(model="m")

    assert _score(scorer, better) == 1.0
    assert _score(scorer, worse) == 0.0


@pytest.mark.parametrize("judge", ["closed_qa", "sql_match", "possible"])
def test_a_yes_no_judge_scores_yes_as_one(judge: str) -> None:
    scorer = getattr(scorers, judge)(model="m")

    assert _score(scorer, "Y") == 1.0
    assert _score(scorer, "N") == 0.0


@pytest.mark.parametrize("judge", ["security", "moderation"])
def test_a_safety_judge_scores_a_clean_output_as_one(judge: str) -> None:
    # Inverted on purpose: higher is better everywhere, so "nothing found" is 1.
    scorer = getattr(scorers, judge)(model="m")

    assert _score(scorer, "N") == 1.0
    assert _score(scorer, "Y") == 0.0


@pytest.mark.parametrize("judge", ["translation", "humor"])
def test_a_rating_judge_spreads_one_to_five_across_the_range(judge: str) -> None:
    scorer = getattr(scorers, judge)(model="m")

    assert [_score(scorer, str(n)) for n in range(1, 6)] == [0.0, 0.25, 0.5, 0.75, 1.0]


def test_the_prompt_carries_the_rubric_and_the_row() -> None:
    prompt = _spec(scorers.closed_qa(model="m")).prompt_fn(
        None, input="2 + 2?", output="4", criteria="Arithmetically correct."
    )

    assert "## input" in prompt and "2 + 2?" in prompt
    assert "## criteria" in prompt and "Arithmetically correct." in prompt
    assert "Y." in prompt and "N." in prompt
    assert '"choice"' in prompt and '"rationale"' in prompt


def test_inputs_and_name_can_be_renamed() -> None:
    scorer = scorers.sql_match(
        model="m", name="query-match", inputs=("q", "got", "want")
    )

    assert scorer._explicit_name == "query-match"
    assert _spec(scorer).inputs == ("q", "got", "want")


def _confidence(scorer, choice: str, probabilities: dict) -> float:
    return _graded(scorer, choice, probabilities)["confidence"]


@pytest.mark.parametrize("judge", _JUDGES)
def test_a_judge_that_gives_no_probabilities_is_unsure(judge: str) -> None:
    scorer = getattr(scorers, judge)(model="m")

    assert _confidence(scorer, "Y", {}) == 0.0


def test_confidence_is_the_share_on_the_verdict_given() -> None:
    scorer = scorers.closed_qa(model="m")

    assert _confidence(scorer, "Y", {"Y": 0.8, "N": 0.2}) == pytest.approx(0.8)
    # Unnormalized and differently cased still reads as a distribution.
    assert _confidence(scorer, "y.", {"y": 3, "n": 1}) == pytest.approx(0.75)
    # Choosing the letter it rated less likely is itself a reason to escalate.
    assert _confidence(scorer, "Y", {"Y": 0.3, "N": 0.7}) == pytest.approx(0.3)
    # Letters outside the rubric are ignored rather than diluting the verdict.
    assert _confidence(scorer, "Y", {"Y": 0.5, "Z": 0.5}) == pytest.approx(1.0)


def test_the_prompt_asks_for_a_probability_per_letter() -> None:
    prompt = _spec(scorers.closed_qa(model="m")).prompt_fn(
        None, input="q", output="a", criteria="c"
    )

    assert '"probabilities"' in prompt
