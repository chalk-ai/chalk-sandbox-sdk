"""Dataclass judges work without importing Pydantic in the SDK environment."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
import types
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import ClassVar, Literal

import pytest

from chalkcompute import scorers
from chalkcompute._evaluation import EvaluationScorerResult

pytestmark = pytest.mark.usefixtures("no_deploy")


class Verdict(str, Enum):
    GOOD = "good"
    BAD = "bad"


@dataclass(frozen=True, slots=True)
class Detail:
    reason: str = field(metadata={"description": "Explain the grade."})
    verdict: Verdict


@dataclass(frozen=True, slots=True)
class Grade:
    score: float
    details: list[Detail]
    confidence: Literal["high", "low"] | None = None
    tags: list[str] = field(default_factory=list)
    category: ClassVar[str] = "quality"


REPLY = {"score": 0.5, "details": [{"reason": "ok", "verdict": "good"}]}


def judge(**kwargs):
    return scorers.llm_judge(Grade, model="test-model", **kwargs)


def test_dataclass_schema_and_metadata(monkeypatch):
    calls = []

    def complete(spec, messages):
        calls.append(messages)
        return REPLY

    monkeypatch.setattr(scorers, "_complete", complete)
    scorer = judge()
    assert scorer._explicit_name == "grade"
    assert scorer._func(output="done") == EvaluationScorerResult(
        score=0.5,
        metadata={"details": REPLY["details"], "confidence": None, "tags": []},
    )
    schema = json.loads(scorer._func.__chalk_scorer_response_schema__)
    assert set(schema["properties"]) == {"details", "confidence", "tags"}
    assert schema["required"] == ["details"]
    assert (
        schema["$defs"]["Detail"]["properties"]["reason"]["description"]
        == "Explain the grade."
    )
    assert '"$defs"' in calls[0][0]["content"]
    assert "Explain the grade." in calls[0][0]["content"]


def test_parse_fn_can_return_a_dataclass_instance(monkeypatch):
    install_openai(monkeypatch, content="unstructured")
    scorer = judge(parse_fn=lambda text: Grade(1, [Detail(text, Verdict.GOOD)]))
    assert scorer._func(output="done").metadata["details"] == [
        {"reason": "unstructured", "verdict": "good"}
    ]


@pytest.mark.parametrize(
    "reply",
    [
        {"details": []},
        {**REPLY, "score": "half"},
        {**REPLY, "score": True},
        {**REPLY, "extra": 1},
        {**REPLY, "details": [{"reason": 4, "verdict": "good"}]},
        {**REPLY, "details": [{"reason": "ok", "verdict": "invalid"}]},
        {**REPLY, "confidence": "maybe"},
        Grade(0.5, [Detail(4, Verdict.GOOD)]),
    ],
)
def test_invalid_dataclass_reply_fails_the_row(monkeypatch, reply):
    monkeypatch.setattr(scorers, "_complete", lambda *args: reply)
    with pytest.raises((TypeError, ValueError)):
        judge()._func(output="done")


def test_requires_score_and_a_class():
    @dataclass
    class NoScore:
        reason: str

    with pytest.raises(ValueError, match="score"):
        scorers.llm_judge(NoScore, model="m")
    with pytest.raises(TypeError, match="class"):
        scorers.llm_judge(Grade(1, []), model="m")


@pytest.mark.parametrize("parsed_instance", [False, True])
def test_dataclass_post_init_runs_once(monkeypatch, parsed_instance):
    calls = []

    @dataclass
    class CheckedGrade:
        score: float

        def __post_init__(self):
            calls.append(self.score)
            if not 0 <= self.score <= 1:
                raise ValueError("score out of range")

    install_openai(monkeypatch, content='{"score": 1}')
    scorer = scorers.llm_judge(
        CheckedGrade,
        model="m",
        parse_fn=(lambda text: CheckedGrade(**json.loads(text)))
        if parsed_instance
        else None,
    )
    assert scorer._func(output="done") == EvaluationScorerResult(score=1, metadata=None)
    assert calls == [1]
    install_openai(monkeypatch, content='{"score": 2}')
    with pytest.raises(ValueError, match="score out of range"):
        scorer._func(output="done")


def install_openai(monkeypatch, *, content=None, refusal=None, finish_reason="stop"):
    requests = []

    def create(**request):
        requests.append(request)
        message = types.SimpleNamespace(content=content, refusal=refusal)
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(message=message, finish_reason=finish_reason)
            ]
        )

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
    )
    monkeypatch.setitem(
        sys.modules, "openai", types.SimpleNamespace(OpenAI=lambda **kwargs: client)
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return requests


def test_dataclass_structured_completion(monkeypatch):
    requests = install_openai(monkeypatch, content=json.dumps(REPLY))
    scorer = judge(completion_kwargs={"temperature": 0})
    result = scorer._func(output="done")
    assert result.score == 0.5
    request = requests[0]
    assert request["temperature"] == 0
    assert request["model"] == "test-model"
    response_format = request["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["required"] == ["score", "details", "confidence", "tags"]
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["Detail"]["additionalProperties"] is False
    assert schema["$defs"]["Detail"]["properties"]["verdict"]["enum"] == ["good", "bad"]


@pytest.mark.parametrize("content,refusal", [(None, "refused"), ("", None)])
def test_dataclass_refusal_or_empty_reply(monkeypatch, content, refusal):
    install_openai(monkeypatch, content=content, refusal=refusal)
    with pytest.raises(RuntimeError, match="no grade"):
        judge()._func(output="done")


def test_dataclass_invalid_json(monkeypatch):
    install_openai(monkeypatch, content="not json")
    with pytest.raises(json.JSONDecodeError):
        judge()._func(output="done")


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_dataclass_incomplete_reply(monkeypatch, finish_reason):
    install_openai(monkeypatch, content=json.dumps(REPLY), finish_reason=finish_reason)
    with pytest.raises(RuntimeError, match="incomplete grade"):
        judge()._func(output="done")


@dataclass
class RecursiveGrade:
    score: float
    children: list[RecursiveGrade] = field(default_factory=list)


def test_recursive_dataclass(monkeypatch):
    monkeypatch.setattr(
        scorers, "_complete", lambda *args: {"score": 1, "children": [{"score": 0}]}
    )
    scorer = scorers.llm_judge(RecursiveGrade, model="m")
    result = scorer._func(output="done")
    assert result.metadata == {"children": [{"score": 0, "children": []}]}
    schema = scorers._json_schema(RecursiveGrade)
    assert schema["properties"]["children"]["items"] == {
        "$ref": "#/$defs/RecursiveGrade"
    }


def test_dataclass_judge_without_pydantic():
    # A fresh interpreter catches accidental imports, including transitive ones.
    code = """
import sys
import importlib.abc
class BlockPydantic(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split(".")[0] in {"pydantic", "pydantic_core"}:
            raise ImportError("Pydantic is not installed")
sys.meta_path.insert(0, BlockPydantic())
from dataclasses import dataclass
from chalkcompute import scorers
from chalkcompute._function import RemoteFunction
RemoteFunction._start_background_deploy = lambda self: None
scorers.should_strip_source = lambda: False
@dataclass
class Grade:
    score: float
    reason: str
judge = scorers.llm_judge(Grade, model="m")
scorers._complete = lambda *args: {"score": 1, "reason": "ok"}
result = judge._func(output="done")
assert result.score == 1 and result.metadata == {"reason": "ok"}
assert not any(name == "pydantic" or name.startswith("pydantic.") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "OTEL_SDK_DISABLED": "true"},
    )
    assert result.returncode == 0, result.stderr


def test_pyproject_does_not_declare_pydantic():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())[
        "project"
    ]
    dependencies = project["dependencies"] + [
        dep for extra in project["optional-dependencies"].values() for dep in extra
    ]
    assert not any(dep.lower().startswith("pydantic") for dep in dependencies)
