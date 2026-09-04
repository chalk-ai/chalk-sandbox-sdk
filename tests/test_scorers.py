"""Tests for ``chalkcompute.scorers.llm_judge``."""

from __future__ import annotations

import inspect
import json
import sys
import types
import typing

import pyarrow as pa
import pytest
from pydantic import BaseModel, Field, ValidationError

from chalkcompute import scorers
from chalkcompute._evaluation import EvaluationScorerResult
from chalkcompute._function import (
    SCORER_RESPONSE_SCHEMA_METADATA_KEY,
    RemoteFunction,
    _build_schemas,
    _generate_handler_shim,
)
from chalkcompute._secret import Secret


class Grade(BaseModel):
    score: float = Field(ge=0, le=1, description="Overall quality.")
    directness: int = Field(ge=0, le=2, description="Shortest path.")
    reason: str


REPLY = {"score": 0.5, "directness": 2, "reason": "ok"}


@pytest.fixture
def no_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RemoteFunction, "_deploy", lambda self, **_: self)


@pytest.fixture
def completion(monkeypatch: pytest.MonkeyPatch):
    """Stub the HTTP call; returns the captured requests and lets tests set the reply."""
    calls: list[tuple[str, dict, str]] = []
    state = {"content": json.dumps(REPLY)}

    def fake_post(url, body, api_key):
        calls.append((url, body, api_key))
        return {"choices": [{"message": {"content": state["content"]}}]}

    monkeypatch.setattr(scorers, "_post_json", fake_post)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return calls, state


def _judge(**overrides):
    kwargs: dict = dict(
        name="trace-quality",
        response_model=Grade,
        model="gpt-5",
        instructions="Be strict.",
    )
    kwargs.update(overrides)
    return scorers.llm_judge(**kwargs)


def test_rejects_non_pydantic_response_model() -> None:
    with pytest.raises(TypeError, match="pydantic"):
        scorers.llm_judge(name="j", response_model=dict, model="m")


def test_requires_a_score_field() -> None:
    class NoScore(BaseModel):
        reason: str

    with pytest.raises(ValueError, match="score"):
        scorers.llm_judge(name="j", response_model=NoScore, model="m")


def test_rejects_invalid_input_names() -> None:
    with pytest.raises(ValueError, match="column"):
        _judge(inputs=("not a column",))


def test_generated_function_is_a_scorer_defined_in_the_caller(no_deploy) -> None:
    judge = _judge(inputs=("output", "expected"))
    fn = judge._func

    assert isinstance(judge, RemoteFunction)
    assert list(inspect.signature(fn).parameters) == ["output", "expected"]
    hints = typing.get_type_hints(fn)
    assert hints["output"] is str and hints["return"] is EvaluationScorerResult
    assert inspect.getfile(fn) == __file__ and fn.__module__ == __name__
    assert getattr(sys.modules[__name__], "_chalk_llm_judge_trace_quality") is judge

    _, output_schema = _build_schemas(fn)
    declared = json.loads(output_schema.metadata[SCORER_RESPONSE_SCHEMA_METADATA_KEY])
    assert list(declared["properties"]) == ["score", "directness", "reason"]
    assert declared["properties"]["directness"]["maximum"] == 2


def test_api_key_secret_is_attached_and_named(no_deploy) -> None:
    secret = Secret.from_chalk_env("OPENAI_API_TOKEN", alias="OPENAI_API_KEY")
    judge = _judge(api_key=secret)

    assert secret in judge._secrets
    assert judge._func.__globals__["_spec"].api_key_env == "OPENAI_API_KEY"
    assert scorers._secret_env_var(Secret.from_local_env("MY_KEY")) == "MY_KEY"
    assert scorers._secret_env_var(Secret.from_chalk_env("K", prefix="P_")) == "P_K"


def test_judge_requests_structured_output_and_splits_score(
    no_deploy, completion
) -> None:
    calls, _ = completion
    judge = _judge(max_tokens=300, temperature=0)

    result = judge._func(output="done")

    assert result == EvaluationScorerResult(
        score=0.5, metadata={"directness": 2, "reason": "ok"}
    )
    url, body, api_key = calls[0]
    assert url == "https://api.openai.com/v1/chat/completions" and api_key == "sk-test"
    assert (
        body["model"] == "gpt-5"
        and body["max_tokens"] == 300
        and body["temperature"] == 0
    )
    schema = body["response_format"]["json_schema"]
    assert schema["strict"] is True and schema["name"] == "Grade"
    assert schema["schema"]["additionalProperties"] is False
    assert schema["schema"]["required"] == ["score", "directness", "reason"]
    system, user = body["messages"]
    assert system == {"role": "system", "content": "Be strict."}
    assert "- directness (integer, 0 to 2): Shortest path." in user["content"]
    assert user["content"].endswith("## output\ndone")


def test_invalid_reply_fails_the_row(no_deploy, completion) -> None:
    _, state = completion
    state["content"] = json.dumps({"score": 0.5, "directness": 7, "reason": "ok"})

    with pytest.raises(ValidationError, match="directness"):
        _judge()._func(output="done")


def test_prompt_and_parse_hooks(no_deploy, completion) -> None:
    calls, state = completion
    state["content"] = "Reasoning: fine\n" + json.dumps(REPLY)

    def prompt_fn(response_model, output):
        return f"grade {response_model.__name__}: {output}"

    def parse_fn(text):
        return json.loads(text.split("\n", 1)[1])

    judge = _judge(prompt_fn=prompt_fn, parse_fn=parse_fn, instructions="")
    result = judge._func(output="done")

    assert result.score == 0.5
    assert calls[0][1]["messages"] == [{"role": "user", "content": "grade Grade: done"}]


def test_custom_api_server_and_missing_key(no_deploy, completion, monkeypatch) -> None:
    calls, _ = completion
    judge = _judge(api_server="https://vllm.local/v1/")
    judge._func(output="done")
    assert calls[0][0] == "https://vllm.local/v1/chat/completions"
    assert "max_tokens" not in calls[0][1] and "temperature" not in calls[0][1]

    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        judge._func(output="done")


def test_handler_shim_runs_the_generated_judge(no_deploy, completion) -> None:
    judge = _judge()
    module = types.ModuleType("user_module")
    setattr(module, judge._func_name, judge._func)
    sys.modules["user_module"] = module
    ns: dict = {}
    exec(compile(_generate_handler_shim(judge._func_name), "<shim>", "exec"), ns)

    results = ns["handler"]({"output": pa.array(["done"])}, None)

    row = results[0].as_py()
    assert row["score"] == 0.5
    assert json.loads(row["metadata"]) == {"directness": 2, "reason": "ok"}
    assert results[0].type.equals(EvaluationScorerResult.__chalk_pyarrow_type__())
