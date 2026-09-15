"""Prebuilt LLM judges: :func:`llm_judge` with a canned rubric and prompt.

Each one asks the model to pick a letter rather than invent a number, and maps
that letter to a score. The letter and the model's reasoning are kept as row
metadata, so a run shows why it graded the way it did.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from chalkcompute._function import RemoteFunction, _NoOpFunction
from chalkcompute.scorers import llm_judge

_GRADER = (
    "You are grading one row of an evaluation. Judge only what you are given, "
    "follow the rubric exactly, and answer with the JSON object it asks for."
)


def _sections(columns: Mapping[str, Any]) -> str:
    return "\n\n".join(f"## {column}\n{value}" for column, value in columns.items())


def _rubric_prompt(intro: str, rubric: str, columns: Mapping[str, Any]) -> str:
    return f"{intro}\n\n{rubric}\n\n{_sections(columns)}"


def factuality(
    *,
    model: str,
    name: str = "factuality",
    inputs: Sequence[str] = ("input", "output", "expected"),
    **judge_kwargs: Any,
) -> RemoteFunction | _NoOpFunction:
    """Deploy a judge that grades an answer against a reference answer.

    The model classifies how the output relates to ``expected`` — a subset of
    it, a superset, the same content, a conflict, or a differently worded
    equivalent — and that choice becomes the score. A conflict scores 0.0;
    agreeing content scores 1.0 whether or not the wording matches.

    ``inputs`` names the question column, the answer to grade, and the
    reference answer. Other keywords go to :func:`llm_judge`.
    """
    from pydantic import BaseModel, computed_field

    class Factuality(BaseModel):
        """How the submitted answer relates to the reference answer."""

        choice: str
        rationale: str

        @computed_field  # type: ignore[prop-decorator]
        @property
        def score(self) -> float:
            return _FACTUALITY_SCORES.get(self.choice.strip().upper()[:1], 0.0)

    return llm_judge(
        Factuality,
        model=model,
        name=name,
        inputs=inputs,
        instructions=_GRADER,
        prompt_fn=_factuality_prompt,
        _frame_depth=3,
        **judge_kwargs,
    )


_FACTUALITY_SCORES = {"A": 0.4, "B": 0.6, "C": 1.0, "D": 0.0, "E": 1.0}

_FACTUALITY_RUBRIC = """\
Compare the submitted answer with the reference answer and pick one letter:

A. The submission is a subset of the reference and fully agrees with it.
B. The submission is a superset of the reference and fully agrees with it.
C. The submission contains the same content as the reference.
D. The submission disagrees with the reference.
E. The submission and reference differ only in wording or formatting.

Reply with a JSON object: {"choice": "<letter>", "rationale": "<one sentence>"}\
"""


def _factuality_prompt(_response_model: Any, **columns: Any) -> str:
    return _rubric_prompt(
        "Grade a submitted answer against a reference answer.",
        _FACTUALITY_RUBRIC,
        columns,
    )
