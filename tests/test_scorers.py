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
    FunctionBuildError,
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


@pytest.fixture(autouse=True)
def no_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never start a deploy: a background thread outliving the test would reach
    the real one after monkeypatch had restored it."""
    monkeypatch.setattr(RemoteFunction, "_start_background_deploy", lambda self: None)
    monkeypatch.setattr(scorers, "should_strip_source", lambda: False)


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
    kwargs: dict = dict(model="gpt-5", instructions="Be strict.")
    kwargs.update(overrides)
    return scorers.llm_judge(kwargs.pop("response_model", Grade), **kwargs)


def test_rejects_non_pydantic_response_model() -> None:
    with pytest.raises(TypeError, match="pydantic"):
        scorers.llm_judge(dict, model="m")


def test_requires_a_score_field() -> None:
    class NoScore(BaseModel):
        reason: str

    with pytest.raises(ValueError, match="score"):
        scorers.llm_judge(NoScore, model="m")


@pytest.mark.parametrize(
    ("inputs", "error", "match"),
    [
        (("not a column",), ValueError, "identifier"),
        (("class",), ValueError, "keyword"),
        (("_judge",), ValueError, "reserved"),
        (("_spec",), ValueError, "reserved"),
        (("out", "out"), ValueError, "duplicate"),
        ((), ValueError, "at least one"),
        ("output", TypeError, "sequence"),
    ],
)
def test_rejects_unusable_input_names(inputs, error, match) -> None:
    with pytest.raises(error, match=match):
        _judge(inputs=inputs)


def test_rejects_mapping_fields_strict_mode_cannot_express() -> None:
    class WithMapping(BaseModel):
        score: float
        labels: dict[str, str]

    with pytest.raises(ValueError, match="mapping"):
        _judge(response_model=WithMapping)


def test_rejects_strip_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scorers, "should_strip_source", lambda: True)

    with pytest.raises(FunctionBuildError, match="strip mode"):
        _judge()


def test_name_defaults_to_the_model_in_kebab_case() -> None:
    class TraceQuality(BaseModel):
        score: float

    assert _judge(response_model=TraceQuality)._explicit_name == "trace-quality"
    assert _judge(name="explicit")._explicit_name == "explicit"


def test_generated_function_is_a_scorer_defined_in_the_caller() -> None:
    judge = _judge(name="trace-quality", inputs=("output", "expected"))
    fn = judge._func

    assert isinstance(judge, RemoteFunction)
    assert list(inspect.signature(fn).parameters) == ["output", "expected"]
    hints = typing.get_type_hints(fn)
    assert hints["output"] is str and hints["return"] is EvaluationScorerResult
    assert inspect.getfile(fn) == __file__ and fn.__module__ == __name__
    assert getattr(sys.modules[__name__], "_chalk_llm_judge_trace_quality") is judge

    _, output_schema = _build_schemas(fn)
    # The declaration describes the metadata: the score has its own column.
    declared = json.loads(output_schema.metadata[SCORER_RESPONSE_SCHEMA_METADATA_KEY])
    assert list(declared["properties"]) == ["directness", "reason"]
    assert declared["properties"]["directness"]["maximum"] == 2


def test_api_key_secret_is_attached_and_named() -> None:
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


def test_invalid_reply_fails_the_row(completion) -> None:
    _, state = completion
    state["content"] = json.dumps({"score": 0.5, "directness": 7, "reason": "ok"})

    with pytest.raises(ValidationError, match="directness"):
        _judge()._func(output="done")


def test_prompt_and_parse_hooks(completion) -> None:
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


def test_custom_api_server_and_missing_key(completion, monkeypatch) -> None:
    calls, _ = completion
    judge = _judge(api_server="https://vllm.local/v1/")
    judge._func(output="done")
    assert calls[0][0] == "https://vllm.local/v1/chat/completions"
    assert "max_tokens" not in calls[0][1] and "temperature" not in calls[0][1]

    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        judge._func(output="done")


def test_handler_shim_runs_the_generated_judge(completion) -> None:
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


def test_post_json_sends_bearer_and_environment_headers(monkeypatch) -> None:
    import io
    import urllib.request

    seen: dict = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        seen["headers"] = dict(request.header_items())
        seen["body"] = json.loads(request.data)
        return Response(b'{"choices": []}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-123")

    assert scorers._post_json("https://x/v1/chat/completions", {"a": 1}, "sk") == {
        "choices": []
    }
    assert seen["body"] == {"a": 1}
    assert seen["headers"]["Authorization"] == "Bearer sk"
    assert seen["headers"]["X-chalk-env-id"] == "env-123"


def test_strict_schema_drops_null_defaults(completion) -> None:
    from typing import Optional

    class WithOptional(BaseModel):
        score: float
        confidence: Optional[float] = None

    _judge(response_model=WithOptional)._func(output="done")

    schema = completion[0][0][1]["response_format"]["json_schema"]["schema"]
    assert "default" not in schema["properties"]["confidence"]
    assert schema["required"] == ["score", "confidence"]


def test_aliased_fields_serialize_the_way_the_schema_names_them(completion) -> None:
    calls, state = completion

    class Aliased(BaseModel):
        score: float = Field(alias="overall")
        directness: int = Field(alias="how_direct", ge=0, le=2)

    state["content"] = json.dumps({"overall": 0.5, "how_direct": 2})
    judge = _judge(response_model=Aliased)

    result = judge._func(output="done")

    # The score comes out of its aliased key; metadata carries the alias the
    # stamped schema declares, and never the score.
    assert result == EvaluationScorerResult(score=0.5, metadata={"how_direct": 2})
    declared = json.loads(judge._func.__chalk_scorer_response_schema__)
    assert list(declared["properties"]) == ["how_direct"]
    assert declared["required"] == ["how_direct"]
    # The model is still asked for the full response, aliases included.
    schema = calls[0][1]["response_format"]["json_schema"]["schema"]
    assert list(schema["properties"]) == ["overall", "how_direct"]
