"""Tests for ``chalkcompute.scorers.factuality``."""

from __future__ import annotations

import json

import pytest

from chalkcompute import scorers

pytestmark = pytest.mark.usefixtures("no_deploy")


def _spec(scorer):
    return scorer._func.__globals__["__chalk_spec__"]


def _graded(scorer, choice: str) -> dict:
    model = _spec(scorer).response_model
    return scorers._dump(model(choice=choice, rationale="because"))


def test_deploys_a_judge_over_the_question_answer_and_reference() -> None:
    scorer = scorers.factuality(model="gpt-4o-mini")

    assert scorer._explicit_name == "factuality"
    assert _spec(scorer).inputs == ("input", "output", "expected")
    assert _spec(scorer).model == "gpt-4o-mini"
    assert scorers.factuality(model="m", name="graded")._explicit_name == "graded"
    assert scorers.factuality(model="m", inputs=("q", "a", "gold")) is not None


@pytest.mark.parametrize(
    ("choice", "score"),
    [("A", 0.4), ("B", 0.6), ("C", 1.0), ("D", 0.0), ("E", 1.0)],
)
def test_each_choice_maps_to_its_score(choice: str, score: float) -> None:
    assert _graded(scorers.factuality(model="m"), choice)["score"] == score


def test_an_unusable_choice_scores_zero_rather_than_failing_the_row() -> None:
    assert _graded(scorers.factuality(model="m"), "nonsense")["score"] == 0.0


def test_the_choice_and_reasoning_are_kept_as_metadata() -> None:
    scorer = scorers.factuality(model="m")
    graded = _graded(scorer, "c")

    # Lowercase from the model still grades, and the raw reply is what is kept.
    assert graded["score"] == 1.0
    assert graded["choice"] == "c"
    assert graded["rationale"] == "because"
    schema = json.loads(scorer._func.__chalk_scorer_response_schema__)
    assert sorted(schema["properties"]) == ["choice", "rationale"]


def test_the_scorer_is_declared_in_the_callers_module_not_the_judges_one() -> None:
    # The pod re-imports the module the scorer was declared in and expects the
    # call that made it to run again, which only happens in the caller's file.
    scorer = scorers.factuality(model="m")

    assert scorer._func.__globals__["__name__"] == __name__


def test_the_prompt_carries_the_rubric_and_the_row() -> None:
    prompt = _spec(scorers.factuality(model="m")).prompt_fn(
        None, input="What is the capital?", output="Paris", expected="Paris, France"
    )

    assert "## input" in prompt and "What is the capital?" in prompt
    assert "## output" in prompt and "Paris" in prompt
    assert "## expected" in prompt and "Paris, France" in prompt
    for letter in ("A.", "B.", "C.", "D.", "E."):
        assert letter in prompt
