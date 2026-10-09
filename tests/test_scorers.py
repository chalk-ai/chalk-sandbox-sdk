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
from chalkcompute._trace import Trace


class Grade(BaseModel):
    score: float = Field(ge=0, le=1, description="Overall quality.")
    directness: int = Field(ge=0, le=2, description="Shortest path.")
    reason: str


REPLY = {"score": 0.5, "directness": 2, "reason": "ok"}

pytestmark = pytest.mark.usefixtures("no_deploy")


@pytest.fixture
def completion(monkeypatch: pytest.MonkeyPatch):
    """Stand in for the model call: records the messages, returns a settable reply."""
    calls: list[list[dict[str, str]]] = []
    state: dict = {"reply": dict(REPLY)}

    def fake_complete(spec, messages):
        calls.append(messages)
        return state["reply"]

    monkeypatch.setattr(scorers, "_complete", fake_complete)
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
    ("inputs", "error"),
    [
        (("not a column",), ValueError),
        (("class",), ValueError),
        (("__chalk_spec__",), ValueError),
        (("out", "out"), ValueError),
        ((), ValueError),
        ("output", TypeError),
    ],
)
def test_rejects_unusable_input_names(inputs, error) -> None:
    with pytest.raises(error):
        _judge(inputs=inputs)


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

    # The declaration describes the metadata: the score has its own column.
    _, output_schema = _build_schemas(fn)
    declared = json.loads(output_schema.metadata[SCORER_RESPONSE_SCHEMA_METADATA_KEY])
    assert list(declared["properties"]) == ["directness", "reason"]
    assert declared["properties"]["directness"]["maximum"] == 2


def test_judges_in_one_module_keep_separate_specs(completion) -> None:
    calls, _ = completion
    strict = _judge(name="strict", instructions="Be strict.")
    lenient = _judge(name="lenient", instructions="Be lenient.", model="gpt-4.1")

    strict._func(output="a")
    lenient._func(output="b")

    # Each generated function closes over its own namespace, so the module
    # can hold any number of judges.
    assert [c[0]["content"] for c in calls] == ["Be strict.", "Be lenient."]
    specs = [f._func.__globals__["__chalk_spec__"] for f in (strict, lenient)]
    assert [s.model for s in specs] == ["gpt-5", "gpt-4.1"]
    module = sys.modules[__name__]
    assert module._chalk_llm_judge_strict is strict
    assert module._chalk_llm_judge_lenient is lenient


def test_api_key_secret_is_attached_and_named() -> None:
    secret = Secret.from_chalk_env("OPENAI_API_TOKEN", alias="OPENAI_API_KEY")
    judge = _judge(api_key=secret)

    assert secret in judge._secrets
    assert judge._func.__globals__["__chalk_spec__"].api_key_env == "OPENAI_API_KEY"
    assert scorers._secret_env_var(Secret.from_local_env("MY_KEY")) == "MY_KEY"
    assert scorers._secret_env_var(Secret.from_chalk_env("K", prefix="P_")) == "P_K"


def test_judge_splits_the_score_from_the_metadata(completion) -> None:
    calls, _ = completion

    result = _judge()._func(output="done")

    assert result == EvaluationScorerResult(
        score=0.5, metadata={"directness": 2, "reason": "ok"}
    )
    system, user = calls[0]
    assert system == {"role": "system", "content": "Be strict."}
    assert '"directness"' in user["content"] and "Shortest path." in user["content"]
    assert user["content"].endswith("## output\n\ndone")


def test_invalid_reply_fails_the_row(completion) -> None:
    _, state = completion
    state["reply"] = {"score": 0.5, "directness": 7, "reason": "ok"}

    with pytest.raises(ValidationError, match="directness"):
        _judge()._func(output="done")


def test_prompt_fn_replaces_the_default_prompt(completion) -> None:
    calls, _ = completion

    def prompt_fn(response_model, output):
        return f"grade {response_model.__name__}: {output}"

    _judge(prompt_fn=prompt_fn, instructions="")._func(output="done")

    assert calls[0] == [{"role": "user", "content": "grade Grade: done"}]


def test_trace_input_is_declared_as_an_optional_trace() -> None:
    """The runner binds `trace` to a snapshot; it is null without telemetry."""
    fn = _judge(inputs=("output", "trace"))._func

    hints = typing.get_type_hints(fn)
    assert hints["output"] is str and hints["trace"] == Trace | None
    input_schema, _ = _build_schemas(fn)
    trace_column = next(
        column for column in input_schema.columns if column.name == "trace"
    )
    assert trace_column.nullable is True
    assert trace_column.arrow_type.arrow_type_enum.field == "struct"
    children = trace_column.arrow_type.arrow_type_enum.value.sub_field_types
    assert [child.name for child in children] == [
        "version",
        "session_id",
        "ingested_through",
        "captured_at",
    ]


def test_default_prompt_renders_the_trace_as_its_transcript(
    completion, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _ = completion
    monkeypatch.setattr(Trace, "transcript", lambda self, **_: f"turns of {self.id}")

    _judge(inputs=("output", "trace"))._func(output="done", trace=Trace("session-1"))

    _, user = calls[0]
    assert user["content"].endswith(
        "## output\n\ndone\n\n## trace\n\nturns of session-1"
    )


def test_default_prompt_says_when_no_trace_was_recorded(completion) -> None:
    calls, _ = completion

    _judge(inputs=("output", "trace"))._func(output="done", trace=None)

    _, user = calls[0]
    assert user["content"].endswith("## trace\n\n(no trace was recorded for this row)")


def test_prompt_fn_receives_the_trace_handle(completion) -> None:
    calls, _ = completion

    def prompt_fn(response_model, output, trace):
        return f"{output}: {trace.id}"

    _judge(inputs=("output", "trace"), prompt_fn=prompt_fn, instructions="")._func(
        output="done", trace=Trace("session-1")
    )

    assert calls[0] == [{"role": "user", "content": "done: session-1"}]


def test_aliased_fields_serialize_the_way_the_schema_names_them(completion) -> None:
    _, state = completion

    class Aliased(BaseModel):
        score: float = Field(alias="overall")
        directness: int = Field(alias="how_direct", ge=0, le=2)

    state["reply"] = {"overall": 0.5, "how_direct": 2}
    judge = _judge(response_model=Aliased)

    result = judge._func(output="done")

    # The score comes out of its aliased key; metadata carries the alias the
    # stamped schema declares, and never the score.
    assert result == EvaluationScorerResult(score=0.5, metadata={"how_direct": 2})
    declared = json.loads(judge._func.__chalk_scorer_response_schema__)
    assert list(declared["properties"]) == ["how_direct"]
    assert declared["required"] == ["how_direct"]


class _FakeConnection:
    """Stands in for ``ConnectClient``: the credentials a function already holds."""

    instances = 0

    def __init__(self) -> None:
        type(self).instances += 1
        self._access_token = f"chalk-token-{type(self).instances}"
        self._api_server = "https://api.staging.chalk.dev/"
        self._env_id = "env-123"
        self.auth_calls = 0

    def _ensure_auth(self) -> None:
        # The real client re-reads a rotating identity here.
        self.auth_calls += 1


@pytest.fixture
def chalk_credentials(monkeypatch: pytest.MonkeyPatch):
    """A function's own Chalk credentials, and no provider key or endpoint."""
    from chalkcompute import _connect

    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CHALK_ENVIRONMENT_ID",
        "CHALK_ACTIVE_ENVIRONMENT",
    ):
        monkeypatch.delenv(name, raising=False)
    _FakeConnection.instances = 0
    scorers._router_cache.clear()
    monkeypatch.setattr(_connect, "ConnectClient", _FakeConnection)
    monkeypatch.setattr(scorers, "_router_cache", {})
    return _FakeConnection


def test_a_judge_without_a_key_or_url_calls_the_chalk_router(chalk_credentials) -> None:
    spec = _judge()._func.__globals__["__chalk_spec__"]

    assert scorers._client_options(spec) == {
        "api_key": "chalk-token-1",
        "base_url": "https://api.staging.chalk.dev/v1/router",
        "default_headers": {"X-Chalk-Env-Id": "env-123"},
    }


def test_the_router_credential_is_reused_until_it_goes_stale(
    chalk_credentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _judge()._func.__globals__["__chalk_spec__"]
    clock = {"now": 1_000.0}
    monkeypatch.setattr(scorers.time, "monotonic", lambda: clock["now"])

    first = scorers._client_options(spec)
    assert scorers._client_options(spec) == first

    # Re-read well before the credential could expire mid-run.
    clock["now"] += scorers._ROUTER_TOKEN_TTL_SECONDS + 1
    assert scorers._client_options(spec)["api_key"] == "chalk-token-2"


def test_every_call_lets_the_credential_refresh_itself(chalk_credentials) -> None:
    spec = _judge()._func.__globals__["__chalk_spec__"]

    scorers._client_options(spec)
    scorers._client_options(spec)

    # One client, re-authenticated per call: a rotating identity expires on its
    # own schedule, which this cache must not outlive.
    connection = scorers._router_cache["connection"]
    assert _FakeConnection.instances == 1
    assert connection.auth_calls == 2


def test_a_declared_key_that_did_not_arrive_is_reported(
    chalk_credentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    judge = _judge(api_key=Secret.from_chalk_env("PROVIDER_KEY"))
    spec = judge._func.__globals__["__chalk_spec__"]
    monkeypatch.delenv("PROVIDER_KEY", raising=False)

    # Naming a key and not getting one is a misconfiguration, not a reason to
    # spend Chalk's providers instead.
    with pytest.raises(RuntimeError, match="PROVIDER_KEY"):
        scorers._client_options(spec)


def test_an_unreachable_router_says_what_to_pass_instead(
    chalk_credentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chalkcompute import _connect

    def unauthenticated() -> _FakeConnection:
        raise RuntimeError("no credentials")

    monkeypatch.setattr(_connect, "ConnectClient", unauthenticated)
    spec = _judge()._func.__globals__["__chalk_spec__"]

    with pytest.raises(RuntimeError, match="api_key=Secret.from_chalk_env"):
        scorers._client_options(spec)


def test_a_base_url_without_a_key_still_asks_for_one(
    chalk_credentials,
) -> None:
    spec = _judge(base_url="https://vllm.local/v1")._func.__globals__["__chalk_spec__"]

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        scorers._client_options(spec)


def test_client_options(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _judge()._func.__globals__["__chalk_spec__"]
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("CHALK_ENVIRONMENT_ID", raising=False)
    monkeypatch.delenv("CHALK_ACTIVE_ENVIRONMENT", raising=False)

    # A key of the user's own goes to the provider, not through Chalk.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert scorers._client_options(spec) == {"api_key": "sk-test"}

    # The router's base URL and environment header come from the pod's env.
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.chalk.ai/v1/router")
    monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-123")
    assert scorers._client_options(spec) == {
        "api_key": "sk-test",
        "base_url": "https://api.chalk.ai/v1/router",
        "default_headers": {"X-Chalk-Env-Id": "env-123"},
    }
    # An explicit base_url wins over the environment.
    explicit = _judge(base_url="https://vllm.local/v1")._func.__globals__[
        "__chalk_spec__"
    ]
    assert scorers._client_options(explicit)["base_url"] == "https://vllm.local/v1"


class _FakeCompletions:
    """Stands in for ``client.chat.completions``, recording each request."""

    def __init__(self, parsed=None, text=None, refusal=None):
        self.requests: list[dict] = []
        self.parsed, self.text, self.refusal = parsed, text, refusal

    def parse(self, **request):
        self.requests.append(request)
        message = types.SimpleNamespace(parsed=self.parsed, refusal=self.refusal)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    def create(self, **request):
        self.requests.append(request)
        message = types.SimpleNamespace(content=self.text)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


def _install_fake_openai(
    monkeypatch: pytest.MonkeyPatch, completions: _FakeCompletions
):
    def fake_client(**options):
        return types.SimpleNamespace(
            options=options, chat=types.SimpleNamespace(completions=completions)
        )

    monkeypatch.setitem(
        sys.modules, "openai", types.SimpleNamespace(OpenAI=fake_client)
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def test_complete_asks_for_structured_output(monkeypatch: pytest.MonkeyPatch) -> None:
    completions = _FakeCompletions(parsed=Grade(**REPLY))
    _install_fake_openai(monkeypatch, completions)
    spec = _judge(
        completion_kwargs={
            "max_tokens": 300,
            "temperature": 0,
            "reasoning_effort": "low",
        }
    )._func.__globals__["__chalk_spec__"]

    graded = scorers._complete(spec, [{"role": "user", "content": "grade"}])

    assert graded == Grade(**REPLY)
    assert completions.requests == [
        {
            "model": "gpt-5",
            "messages": [{"role": "user", "content": "grade"}],
            "max_tokens": 300,
            "temperature": 0,
            "reasoning_effort": "low",
            "response_format": Grade,
        }
    ]


def test_completion_kwargs_cannot_override_what_the_judge_sets() -> None:
    with pytest.raises(ValueError, match="messages, response_format"):
        _judge(completion_kwargs={"messages": [], "response_format": {}})


def test_complete_falls_back_to_the_beta_parse_on_older_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    beta = _FakeCompletions(parsed=Grade(**REPLY))
    without_parse = types.SimpleNamespace(create=beta.create)

    def fake_client(**options):
        return types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=without_parse),
            beta=types.SimpleNamespace(chat=types.SimpleNamespace(completions=beta)),
        )

    monkeypatch.setitem(
        sys.modules, "openai", types.SimpleNamespace(OpenAI=fake_client)
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    spec = _judge()._func.__globals__["__chalk_spec__"]

    assert scorers._complete(spec, []) == Grade(**REPLY)
    assert beta.requests[0]["response_format"] is Grade


def test_complete_reports_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_openai(monkeypatch, _FakeCompletions(parsed=None, refusal="no"))
    spec = _judge()._func.__globals__["__chalk_spec__"]

    with pytest.raises(RuntimeError, match="no"):
        scorers._complete(spec, [])


def test_complete_hands_raw_text_to_parse_fn(monkeypatch: pytest.MonkeyPatch) -> None:
    completions = _FakeCompletions(text="Reasoning: fine\n" + json.dumps(REPLY))
    _install_fake_openai(monkeypatch, completions)
    spec = _judge(
        parse_fn=lambda text: json.loads(text.split("\n", 1)[1])
    )._func.__globals__["__chalk_spec__"]

    assert scorers._complete(spec, []) == REPLY
    assert "response_format" not in completions.requests[0]


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


def test_handler_shim_decodes_the_trace_column_into_a_handle(
    completion, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session id arrives as a Trace; a null row arrives as None, and both grade."""
    monkeypatch.setattr(Trace, "transcript", lambda self, **_: f"turns of {self.id}")
    judge = _judge(inputs=("output", "trace"))
    module = types.ModuleType("user_module")
    setattr(module, judge._func_name, judge._func)
    sys.modules["user_module"] = module
    ns: dict = {}
    exec(compile(_generate_handler_shim(judge._func_name), "<shim>", "exec"), ns)

    calls, _ = completion
    results = ns["handler"](
        {
            "output": pa.array(["done", "done"]),
            "trace": pa.array(["session-1", None], type=pa.large_utf8()),
        },
        None,
    )

    assert [result.as_py()["score"] for result in results] == [0.5, 0.5]
    assert calls[0][1]["content"].endswith("## trace\n\nturns of session-1")
    assert calls[1][1]["content"].endswith(
        "## trace\n\n(no trace was recorded for this row)"
    )


def test_pydantic_v1_models_work_too(completion) -> None:
    from pydantic.v1 import BaseModel as V1Model
    from pydantic.v1 import Field as V1Field

    class Grade1(V1Model):
        score: float = V1Field(alias="overall")
        directness: int = V1Field(ge=0, le=2)
        reason: str

    _, state = completion
    state["reply"] = {"overall": 0.5, "directness": 2, "reason": "ok"}
    judge = _judge(response_model=Grade1)

    result = judge._func(output="done")

    assert result == EvaluationScorerResult(
        score=0.5, metadata={"directness": 2, "reason": "ok"}
    )
    declared = json.loads(judge._func.__chalk_scorer_response_schema__)
    assert list(declared["properties"]) == ["directness", "reason"]
    assert declared["properties"]["directness"]["maximum"] == 2
