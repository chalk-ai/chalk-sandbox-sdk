"""Scorers you declare instead of write."""

from __future__ import annotations

import difflib
import functools
import json
import keyword
import math
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from chalkcompute import _tracing
from chalkcompute._evaluation import EvaluationScorerResult, SqlExpression
from chalkcompute._function import FunctionBuildError, RemoteFunction, _NoOpFunction
from chalkcompute._function import function as _function
from chalkcompute._image import Image
from chalkcompute._local_dev import should_strip_source
from chalkcompute._naming import sanitize_dns_label
from chalkcompute._secret import Secret
from chalkcompute._trace import Trace

_DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
# The one input the evaluation runner binds itself: the row's session id, which
# a scorer receives as a Trace rather than the string every other input is.
_TRACE_INPUT = "trace"
_NO_TRACE_NOTE = "(no trace was recorded for this row)"
# In the judge's image, not the SDK's. Unbounded, so a user's own image keeps
# the versions it has.
_JUDGE_PACKAGES = ["pydantic", "openai"]
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Chalk's AI router, under the environment's own API server.
_ROUTER_PATH = "/v1/router"
# Well inside the credential's lifetime, so a long scorer run does not carry a
# token past its expiry.
_ROUTER_TOKEN_TTL_SECONDS = 30 * 60
_router_cache: dict[str, Any] = {}
_ROUTER_UNREACHABLE = (
    "llm_judge: no api_key and no base_url, and this environment has no Chalk "
    "credentials to reach the AI router with. Pass api_key=Secret.from_chalk_env(...) "
    "for a provider key, or base_url= for another OpenAI-compatible endpoint."
)


@dataclass(frozen=True)
class _JudgeSpec:
    response_model: Any
    # What the score serializes as; an alias can rename it.
    score_key: str
    model: str
    instructions: str
    inputs: tuple[str, ...]
    prompt_fn: Callable[..., str] | None
    parse_fn: Callable[[str], Any] | None
    base_url: str | None
    api_key_env: str
    # Whether the caller named a key, as opposed to the default env var.
    api_key_declared: bool
    completion_kwargs: dict[str, Any]


@dataclass(frozen=True)
class _EmbeddingSpec:
    model: str
    base_url: str | None
    api_key_env: str


_RESERVED_COMPLETION_KWARGS = frozenset({"model", "messages", "response_format"})


def llm_judge(
    response_model: type,
    /,
    *,
    model: str,
    name: str | None = None,
    instructions: str = "",
    inputs: Sequence[str] = ("output",),
    prompt_fn: Callable[..., str] | None = None,
    parse_fn: Callable[[str], Any] | None = None,
    base_url: str | None = None,
    api_key: Secret | None = None,
    completion_kwargs: Mapping[str, Any] | None = None,
    image: Image | None = None,
    **function_kwargs: Any,
) -> RemoteFunction | _NoOpFunction:
    """Deploy an LLM judge described by a pydantic model.

    ``response_model`` needs a ``score`` field, which becomes the scorer's
    score; its other fields are recorded as row metadata. A reply that fails
    validation fails the row.

    ``inputs`` are the dataset columns to grade (``output`` is the task's).
    Naming ``trace`` among them hands the judge the row's
    :class:`~chalkcompute.Trace`, rendered into the prompt with
    :meth:`~chalkcompute.Trace.transcript`, or ``None`` for a row that recorded
    no telemetry. The default prompt fails the row if raw trace content was
    externalized but not loaded, or was omitted/truncated during recording.
    ``prompt_fn(response_model, **inputs)`` replaces the prompt
    and receives that same handle; ``parse_fn``
    replaces structured outputs. With neither ``base_url`` nor ``api_key`` the
    judge calls Chalk's AI router as the environment itself, so no provider key
    is needed; ``base_url`` takes any other OpenAI-compatible URL and
    ``api_key`` a Secret read in the pod. ``completion_kwargs`` (say
    ``{"temperature": 0, "max_tokens": 400}``) are sent with every request.
    ``name`` defaults to the model class in kebab case; other keywords go to
    :func:`chalkcompute.function`.
    """
    _assert_deployable()
    _assert_response_model(response_model)
    inputs = _validate_inputs(inputs)
    completion_kwargs = _validate_completion_kwargs(completion_kwargs)
    name = name or _default_name(response_model)
    secrets: list[Secret] = []
    api_key_env = _DEFAULT_API_KEY_ENV
    if api_key is not None:
        api_key_env = _secret_env_var(api_key)
        secrets.append(api_key)
    score_key = _serialized_name(response_model, "score")
    spec = _JudgeSpec(
        response_model=response_model,
        score_key=score_key,
        model=model,
        instructions=instructions,
        inputs=inputs,
        prompt_fn=prompt_fn,
        parse_fn=parse_fn,
        base_url=base_url,
        api_key_env=api_key_env,
        api_key_declared=api_key is not None,
        completion_kwargs=completion_kwargs,
    )

    def grade(**columns: Any) -> EvaluationScorerResult:
        return _judge(spec, columns)

    return _declare_scorer(
        name,
        inputs,
        grade,
        image=image,
        function_kwargs=function_kwargs,
        packages=_JUDGE_PACKAGES,
        secrets=secrets,
        stamp={
            "__chalk_scorer_response_schema__": json.dumps(
                _metadata_schema(response_model, score_key)
            )
        },
        extra_globals={"__chalk_spec__": spec},
        prefix="_chalk_llm_judge_",
    )


def _declare_scorer(
    name: str,
    inputs: tuple[str, ...],
    body: Callable[..., EvaluationScorerResult],
    *,
    image: Image | None,
    function_kwargs: dict[str, Any],
    packages: Sequence[str] = (),
    secrets: Sequence[Secret] = (),
    stamp: Mapping[str, Any] | None = None,
    extra_globals: Mapping[str, Any] | None = None,
    prefix: str = "_chalk_scorer_",
    frame_depth: int = 2,
) -> RemoteFunction | _NoOpFunction:
    """Deploy ``body`` as a scorer taking one ``str`` column per ``inputs``.

    ``trace`` is the exception: the evaluation runner binds it to the row's
    session id, so it is declared ``Trace | None`` and arrives as a handle, or
    as ``None`` for a row that recorded no telemetry. The wire type is the same
    ``large_utf8`` either way.

    The generated function is defined in the caller's module so the deploy
    ships that file, where anything the body closes over lives, and the
    pod-side shim imports it back. ``stamp`` sets attributes on it and
    ``extra_globals`` names values in its module namespace.
    """
    frame = sys._getframe(frame_depth)
    caller_module = frame.f_globals.get("__name__", "__main__")
    py_name = prefix + re.sub(r"[^0-9A-Za-z_]", "_", name)
    params = ", ".join(
        f"{column}: __chalk_trace__" if column == _TRACE_INPUT else f"{column}: str"
        for column in inputs
    )
    columns = ", ".join(f"{column}={column}" for column in inputs)
    source = (
        f"def {py_name}({params}) -> __chalk_result__:\n"
        f"    return __chalk_body__({columns})\n"
    )
    namespace: dict[str, Any] = {
        "__name__": caller_module,
        "__chalk_result__": EvaluationScorerResult,
        "__chalk_trace__": Trace | None,
        "__chalk_body__": body,
        **(extra_globals or {}),
    }
    exec(compile(source, frame.f_code.co_filename, "exec"), namespace)
    scorer = namespace[py_name]
    for attribute, value in (stamp or {}).items():
        setattr(scorer, attribute, value)

    base_image = image if image is not None else Image.debian_slim()
    declared = list(function_kwargs.pop("secrets", None) or []) + list(secrets)
    deployed = _function(
        name=name,
        image=base_image.pip_install(list(packages)) if packages else base_image,
        secrets=declared or None,
        **function_kwargs,
    )(scorer)
    module = sys.modules.get(caller_module)
    if module is not None:
        setattr(module, py_name, deployed)
    return deployed


def _assert_deployable(what: str = "cc.scorers.llm_judge") -> None:
    # Stripping removes the caller's chalkcompute import but keeps this call,
    # so the shipped module would not import.
    if should_strip_source():
        raise FunctionBuildError(
            f"{what} cannot be deployed in strip mode "
            "(CHALK_FUNCTION_STRIP_SOURCE, or a 0.0.0 source checkout): the scorer "
            "is generated at import time and stripping removes the chalkcompute "
            "import the deployed module needs. Deploy with a published chalkcompute "
            "installed, or set CHALK_FUNCTION_LOCAL_SDK_COPY=1 with "
            "CHALK_FUNCTION_STRIP_SOURCE=0."
        )


def _assert_response_model(response_model: Any) -> None:
    if not _is_pydantic_model(response_model):
        raise TypeError("response_model must be a pydantic BaseModel class")
    if "score" not in _field_names(response_model):
        raise ValueError(
            "response_model must declare a `score` field; it becomes the scorer's "
            "score and the remaining fields are recorded as metadata"
        )


# pydantic 1 and 2 both work, for parity with chalkpy; the helpers below pick
# the API the installed version has.
def _is_pydantic_model(cls: Any) -> bool:
    return isinstance(cls, type) and (
        hasattr(cls, "model_fields") or hasattr(cls, "__fields__")
    )


def _field_names(cls: Any) -> Any:
    fields = getattr(cls, "model_fields", None)
    return (fields if fields is not None else cls.__fields__).keys()


def _json_schema(cls: Any, *, serialization: bool = False) -> dict[str, Any]:
    if hasattr(cls, "model_json_schema"):
        return cls.model_json_schema(
            mode="serialization" if serialization else "validation"
        )
    return cls.schema(by_alias=True)


def _validate(cls: Any, data: Any) -> Any:
    return (
        cls.model_validate(data)
        if hasattr(cls, "model_validate")
        else cls.parse_obj(data)
    )


def _dump(instance: Any) -> dict[str, Any]:
    if hasattr(instance, "model_dump"):
        return instance.model_dump(mode="json", by_alias=True)
    return json.loads(instance.json(by_alias=True))


def _validate_completion_kwargs(kwargs: Mapping[str, Any] | None) -> dict[str, Any]:
    kwargs = dict(kwargs or {})
    reserved = sorted(_RESERVED_COMPLETION_KWARGS & kwargs.keys())
    if reserved:
        raise ValueError(
            f"llm_judge: completion_kwargs cannot set {', '.join(reserved)}; "
            "the judge sets those itself"
        )
    return kwargs


def _validate_inputs(inputs: Sequence[str]) -> tuple[str, ...]:
    """Column names become parameters of the generated judge."""
    if isinstance(inputs, str):
        raise TypeError(f"inputs must be a sequence of column names, not {inputs!r}")
    columns = tuple(inputs)
    if not columns:
        raise ValueError("inputs must name at least one column")
    for column in columns:
        if (
            not isinstance(column, str)
            or not _IDENTIFIER_RE.match(column)
            or keyword.iskeyword(column)
            or column.startswith("__chalk")
        ):
            raise ValueError(f"input {column!r} cannot be a parameter name")
    if len(set(columns)) != len(columns):
        raise ValueError(f"inputs repeat a column: {columns}")
    return columns


def _default_name(response_model: Any) -> str:
    """``TraceQuality`` -> ``trace-quality``."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", response_model.__name__)
    return sanitize_dns_label(spaced)


def _serialized_name(response_model: Any, field: str) -> str:
    """The key the model serializes ``field`` under."""
    fields = getattr(response_model, "model_fields", None)
    if fields is None:
        return response_model.__fields__[field].alias
    info = fields[field]
    return info.serialization_alias or info.alias or field


def _metadata_schema(response_model: Any, score_key: str) -> dict[str, Any]:
    """What lands in metadata: the model as it serializes, minus the score."""
    schema = dict(_json_schema(response_model, serialization=True))
    schema["properties"] = {
        name: prop
        for name, prop in schema.get("properties", {}).items()
        if name != score_key
    }
    if "required" in schema:
        schema["required"] = [name for name in schema["required"] if name != score_key]
    return schema


def _secret_env_var(secret: Secret) -> str:
    env_var = secret._env_var_name or secret._local_env_var
    if env_var:
        return env_var
    if secret._secret_name:
        base = secret._aliases.get(secret._secret_name, secret._secret_name)
        return f"{secret._prefix or ''}{base}"
    raise ValueError(
        "api_key must name a single env var: use Secret.from_chalk_env or "
        "Secret.from_local_env"
    )


def _judge(spec: _JudgeSpec, columns: Mapping[str, Any]) -> EvaluationScorerResult:
    if spec.prompt_fn is not None:
        prompt = spec.prompt_fn(spec.response_model, **columns)
    else:
        prompt = _default_prompt(spec, columns)
    messages: list[dict[str, str]] = []
    if spec.instructions:
        messages.append({"role": "system", "content": spec.instructions})
    messages.append({"role": "user", "content": prompt})
    graded = _complete(spec, messages)
    if not isinstance(graded, spec.response_model):
        graded = _validate(spec.response_model, graded)
    # By alias, the way the stamped schema names it.
    data = _dump(graded)
    score = data.pop(spec.score_key)
    return EvaluationScorerResult(score=float(score), metadata=data or None)


def _default_prompt(spec: _JudgeSpec, columns: Mapping[str, Any]) -> str:
    fields = _json_schema(spec.response_model).get("properties", {})
    parts = [
        "Grade the material below. Reply with a JSON object matching this schema:",
        json.dumps(fields, indent=1),
    ]
    for column, value in columns.items():
        parts.extend([f"## {column}", _render_input(column, value)])
    return "\n\n".join(parts)


def _render_input(column: str, value: Any) -> str:
    """A prompt section's body: the trace as its transcript, anything else as text."""
    if isinstance(value, Trace):
        return value.transcript(require_content=True)
    if column == _TRACE_INPUT and value is None:
        return _NO_TRACE_NOTE
    return str(value)


def _environment_id() -> str | None:
    return os.environ.get("CHALK_ENVIRONMENT_ID") or os.environ.get(
        "CHALK_ACTIVE_ENVIRONMENT"
    )


def _client_headers(environment_id: str | None) -> dict[str, str]:
    # Attribution belongs to the active row, not the cached router credential.
    headers = _tracing.trace_headers()
    if environment_id is not None:
        headers["X-Chalk-Env-Id"] = environment_id
    session_id = _tracing.current_session_id()
    if session_id:
        headers["X-Chalk-Agent-Session-Id"] = session_id
    return headers


def _client_options(spec: _JudgeSpec | _EmbeddingSpec) -> dict[str, Any]:
    base_url = spec.base_url or os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get(spec.api_key_env)
    # Only a judge that asked for nothing falls back: the router serves
    # completions, and a declared api_key that did not arrive is a
    # misconfiguration to report, not a reason to bill Chalk's providers.
    if (
        base_url is None
        and api_key is None
        and isinstance(spec, _JudgeSpec)
        and not spec.api_key_declared
    ):
        return _router_client_options()
    if not api_key:
        raise RuntimeError(
            f"llm_judge: {spec.api_key_env} is not set in the function environment; "
            "pass api_key=Secret.from_chalk_env(...) to llm_judge"
        )
    options: dict[str, Any] = {"api_key": api_key}
    if base_url:
        options["base_url"] = base_url
    headers = _client_headers(_environment_id())
    if headers:
        options["default_headers"] = headers
    return options


def _router_client_options() -> dict[str, Any]:
    """Chalk's AI router, which takes a Chalk credential rather than a provider key."""
    from chalkcompute._connect import ConnectClient

    now = time.monotonic()
    issued_at = float(_router_cache.get("issued_at", 0.0))
    connection = _router_cache.get("connection")
    # A rotating web identity re-reads its token here, on its own expiry
    # schedule; a client whose credential never refreshes is replaced instead.
    try:
        if connection is None or now - issued_at > _ROUTER_TOKEN_TTL_SECONDS:
            connection = ConnectClient()
            _router_cache.update(connection=connection, issued_at=now)
        connection._ensure_auth()
    except Exception as exc:
        _router_cache.clear()
        raise RuntimeError(_ROUTER_UNREACHABLE) from exc
    token = connection._access_token
    api_server = (connection._api_server or "").rstrip("/")
    if not token or not api_server:
        raise RuntimeError(_ROUTER_UNREACHABLE)
    return {
        "api_key": token,
        "base_url": f"{api_server}{_ROUTER_PATH}",
        "default_headers": _client_headers(
            connection._env_id or _environment_id() or ""
        ),
    }


def _complete(spec: _JudgeSpec, messages: list[dict[str, str]]) -> Any:
    """One chat completion: the parsed model, or ``parse_fn``'s reading of the text."""
    # Present in the judge's image, never in the SDK's environment.
    from openai import OpenAI  # ty: ignore[unresolved-import]

    client = OpenAI(**_client_options(spec))
    request: dict[str, Any] = {
        "model": spec.model,
        "messages": messages,
        **spec.completion_kwargs,
    }
    if spec.parse_fn is not None:
        text = client.chat.completions.create(**request).choices[0].message.content
        return spec.parse_fn(text or "")
    # The client derives the strict schema and validates the reply. parse()
    # left beta in openai 1.92; older clients still have it under beta.
    parse = getattr(client.chat.completions, "parse", None)
    if parse is None:
        parse = client.beta.chat.completions.parse
    message = parse(response_format=spec.response_model, **request).choices[0].message
    if message.parsed is None:
        raise RuntimeError(
            f"llm_judge: the model returned no grade ({message.refusal or 'empty reply'})"
        )
    return message.parsed


# ---------------------------------------------------------------------------
# Escalating judge
# ---------------------------------------------------------------------------

_JEV_INSTRUCTIONS = (
    "You are grading one row of an evaluation. Judge only what you are given, "
    "follow the rubric exactly, and answer with the JSON object it asks for."
)
_JEV_REPLY = (
    'Reply with a JSON object: {"choice": "<label>", "rationale": "<one sentence>", '
    '"probabilities": [{"label": "<label>", "probability": <probability>}, ...]}, '
    "giving your probability for every label."
)
# Written out rather than derived, so declaring the scorer needs no pydantic.
_JEV_METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "choice": {"type": "string"},
        "rationale": {"type": "string"},
        "confidence": {"type": "number"},
        "probabilities": {
            "type": "object",
            "additionalProperties": {"type": "number"},
        },
        "escalated": {"type": "boolean"},
        "judge_model": {"type": "string"},
        "primary_confidence": {"type": "number"},
    },
}


def jev(
    *,
    labels: Mapping[str, float],
    rubric: str,
    model: str,
    fallback_model: str,
    threshold: float = 0.9,
    name: str = "jev",
    inputs: Sequence[str] = ("output",),
    instructions: str = _JEV_INSTRUCTIONS,
    base_url: str | None = None,
    api_key: Secret | None = None,
    completion_kwargs: Mapping[str, Any] | None = None,
    image: Image | None = None,
    **function_kwargs: Any,
) -> RemoteFunction | _NoOpFunction:
    """Deploy a judge that hands the rows it is unsure of to a stronger model.

    The cascade from JEV-as-a-Judge (arXiv 2609.26550): ``model`` picks one of
    ``labels`` for each row and gives a probability for every label. A row
    whose probability on the chosen label is below ``threshold`` is graded
    again by ``fallback_model``, whose grade stands. ``labels`` maps each label
    to its score, and ``rubric`` says what the labels mean.

    Metadata keeps the choice, the rationale, the label probabilities, and
    which model graded the row, so a run shows its own escalation rate.
    Self-reported confidence is not calibrated everywhere (the paper finds it
    overconfident on hard reasoning), so choose ``threshold`` on a small
    labeled sample rather than trusting the default. The remaining keywords
    are as for :func:`llm_judge`.
    """
    _assert_deployable("cc.scorers.jev")
    scores = _validate_labels(labels)
    if isinstance(threshold, bool) or not 0 < threshold <= 1:
        raise ValueError(f"jev: threshold must be in (0, 1], not {threshold!r}")
    if not rubric.strip():
        raise ValueError("jev: rubric must say what the labels mean")
    inputs = _validate_inputs(inputs)
    secrets: list[Secret] = []
    api_key_env = _DEFAULT_API_KEY_ENV
    if api_key is not None:
        api_key_env = _secret_env_var(api_key)
        secrets.append(api_key)
    # The reply model is pydantic, which only the judge's image is sure to
    # have, so it is filled in when a row is graded.
    spec = _JudgeSpec(
        response_model=None,
        score_key="score",
        model=model,
        instructions=instructions,
        inputs=inputs,
        prompt_fn=None,
        parse_fn=None,
        base_url=base_url,
        api_key_env=api_key_env,
        api_key_declared=api_key is not None,
        completion_kwargs=_validate_completion_kwargs(completion_kwargs),
    )
    listed = ", ".join(labels)

    def grade(**columns: Any) -> EvaluationScorerResult:
        parts = [rubric, f"Choose exactly one label: {listed}.", _JEV_REPLY]
        for column, value in columns.items():
            parts.extend([f"## {column}", _render_input(column, value)])
        messages = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": "\n\n".join(parts)},
        ]
        primary = replace(spec, response_model=_jev_reply_model())
        graded = _jev_grade(primary, messages, scores)
        confidence = graded["confidence"]
        escalated = confidence < threshold
        if escalated:
            graded = _jev_grade(
                replace(primary, model=fallback_model), messages, scores
            )
        score = graded.pop("score")
        return EvaluationScorerResult(
            score=score,
            metadata={
                **graded,
                "escalated": escalated,
                "judge_model": fallback_model if escalated else model,
                "primary_confidence": confidence,
            },
        )

    return _declare_scorer(
        name,
        inputs,
        grade,
        image=image,
        function_kwargs=function_kwargs,
        packages=_JUDGE_PACKAGES,
        secrets=secrets,
        stamp={"__chalk_scorer_response_schema__": json.dumps(_JEV_METADATA_SCHEMA)},
        extra_globals={"__chalk_spec__": spec},
    )


def _label(value: str) -> str:
    """A label as the model wrote it, give or take case and a trailing period."""
    return value.strip().strip(".").strip().upper()


def _validate_labels(labels: Mapping[str, float]) -> dict[str, float]:
    """``labels`` keyed the way a reply is read back, each score in ``[0, 1]``."""
    if not isinstance(labels, Mapping) or len(labels) < 2:
        raise ValueError("jev: labels must map at least two labels to their scores")
    scores: dict[str, float] = {}
    for label, score in labels.items():
        key = _label(label) if isinstance(label, str) else ""
        if not key:
            raise ValueError(f"jev: label {label!r} must be a non-empty string")
        if key in scores:
            raise ValueError(f"jev: labels repeat {label!r}, ignoring case")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 0 <= score <= 1
        ):
            raise ValueError(
                f"jev: label {label!r} must score in [0, 1], not {score!r}"
            )
        scores[key] = float(score)
    return scores


@functools.cache
def _jev_reply_model() -> type:
    from pydantic import BaseModel

    class LabelProbability(BaseModel):
        label: str
        probability: float

    class JevReply(BaseModel):
        choice: str
        rationale: str
        # Fixed-shape entries rather than a dict: strict structured outputs
        # accept neither open-ended keys nor an optional field.
        probabilities: list[LabelProbability]

    # The annotations are strings here, and pydantic 1 resolves them against
    # the module rather than this function, so it has to be told the name.
    if not hasattr(JevReply, "model_rebuild"):
        JevReply.update_forward_refs(LabelProbability=LabelProbability)
    return JevReply


def _jev_distribution(
    probabilities: Sequence[Any], scores: Mapping[str, float]
) -> dict[str, float]:
    """The reply's probabilities, normalized, or nothing if they are not usable.

    Usable means one finite, non-negative probability for every label and
    nothing else. Anything short of that reads as unsure, since normalizing a
    partial answer would inflate whatever the model did say.
    """
    given: dict[str, float] = {}
    for entry in probabilities:
        label = _label(entry.label)
        probability = entry.probability
        if (
            label not in scores
            or label in given
            or not math.isfinite(probability)
            or probability < 0
        ):
            return {}
        given[label] = probability
    total = sum(given.values())
    if given.keys() != scores.keys() or total <= 0:
        return {}
    return {label: p / total for label, p in given.items()}


def _jev_grade(
    spec: _JudgeSpec, messages: list[dict[str, str]], scores: Mapping[str, float]
) -> dict[str, Any]:
    """One model's grade: its score, reasoning, and confidence in its choice."""
    reply = _complete(spec, messages)
    distribution = _jev_distribution(reply.probabilities, scores)
    choice = _label(reply.choice)
    return {
        # A choice outside the labels scores 0 and, having no probability,
        # counts as unsure.
        "score": scores.get(choice, 0.0),
        "choice": reply.choice,
        "rationale": reply.rationale,
        # The paper's max probability when the choice is the likeliest label,
        # and lower when the model picked one it rated less likely.
        "confidence": distribution.get(choice, 0.0),
        "probabilities": distribution,
    }


# ---------------------------------------------------------------------------
# Deterministic scorers
# ---------------------------------------------------------------------------


def sql(name: str, expression: str, /) -> SqlExpression:
    """Score rows with a Chalk SQL expression you write yourself.

    ``expression`` is a select item over the evaluation's own columns: every
    dataset column by name, plus ``output``, the task's result for the row. It
    must yield a number. Nothing is deployed; ``name`` labels the result column
    and the row in the compare table.
    """
    return SqlExpression(name, expression)


def _validate_pair(inputs: Sequence[str], what: str) -> tuple[str, str]:
    """The candidate column, then the column it is compared against."""
    columns = _validate_inputs(inputs)
    if len(columns) != 2:
        raise ValueError(
            f"{what}: inputs must name the candidate column and the column it is "
            f"compared against, not {columns}"
        )
    return columns[0], columns[1]


def _text(value: Any) -> str:
    """A missing column reads as empty rather than failing the row."""
    return "" if value is None else str(value)


def _normalized(
    value: Any,
    *,
    case_insensitive: bool = False,
    strip: bool = True,
    normalize_whitespace: bool = False,
) -> str:
    text = _text(value)
    if normalize_whitespace:
        text = " ".join(text.split())
    elif strip:
        text = text.strip()
    return text.casefold() if case_insensitive else text


def exact_match(
    *,
    name: str = "exact-match",
    inputs: Sequence[str] = ("output", "expected"),
    case_insensitive: bool = False,
    strip: bool = True,
    normalize_whitespace: bool = False,
    image: Image | None = None,
    **function_kwargs: Any,
) -> RemoteFunction | _NoOpFunction:
    """Deploy a scorer that gives 1.0 when two columns hold the same text.

    ``inputs`` names the candidate column and then the column it is compared
    against. ``strip`` trims each side, ``normalize_whitespace`` collapses runs
    of whitespace instead, and ``case_insensitive`` folds case. The metadata
    records whether the two matched.
    """
    _assert_deployable("cc.scorers.exact_match")
    candidate, reference = _validate_pair(inputs, "exact_match")

    def compare(**columns: Any) -> EvaluationScorerResult:
        options = {
            "case_insensitive": case_insensitive,
            "strip": strip,
            "normalize_whitespace": normalize_whitespace,
        }
        matched = _normalized(columns[candidate], **options) == _normalized(
            columns[reference], **options
        )
        return EvaluationScorerResult(
            score=1.0 if matched else 0.0, metadata={"matched": matched}
        )

    return _declare_scorer(
        name,
        (candidate, reference),
        compare,
        image=image,
        function_kwargs=function_kwargs,
    )


def _validate_single(inputs: Sequence[str], what: str) -> str:
    """The one column this scorer reads."""
    columns = _validate_inputs(inputs)
    if len(columns) != 1:
        raise ValueError(f"{what}: inputs must name exactly one column, not {columns}")
    return columns[0]


def _levenshtein_distance(a: str, b: str) -> int:
    """Edit distance, keeping one row of the matrix at a time."""
    if a == b:
        return 0
    if not a or not b:
        return len(a) or len(b)
    previous = list(range(len(b) + 1))
    for i, from_a in enumerate(a, start=1):
        current = [i]
        for j, from_b in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (from_a != from_b),
                )
            )
        previous = current
    return previous[-1]


def levenshtein(
    *,
    name: str = "levenshtein",
    inputs: Sequence[str] = ("output", "expected"),
    case_insensitive: bool = False,
    image: Image | None = None,
    **function_kwargs: Any,
) -> RemoteFunction | _NoOpFunction:
    """Deploy a scorer that grades two columns by their edit distance.

    The score is ``1 - distance / len(longer)``, so identical text scores 1.0
    and text sharing nothing scores 0.0. The edit distance itself is recorded
    as metadata. This is the same quantity as ``F.levenshtein_distance``,
    normalized into a score.
    """
    _assert_deployable("cc.scorers.levenshtein")
    candidate, reference = _validate_pair(inputs, "levenshtein")

    def compare(**columns: Any) -> EvaluationScorerResult:
        a = _normalized(columns[candidate], case_insensitive=case_insensitive)
        b = _normalized(columns[reference], case_insensitive=case_insensitive)
        distance = _levenshtein_distance(a, b)
        longest = max(len(a), len(b))
        return EvaluationScorerResult(
            score=1.0 if longest == 0 else 1.0 - distance / longest,
            metadata={"distance": distance},
        )

    return _declare_scorer(
        name,
        (candidate, reference),
        compare,
        image=image,
        function_kwargs=function_kwargs,
    )


def regex_match(
    pattern: str,
    /,
    *,
    name: str = "regex-match",
    inputs: Sequence[str] = ("output",),
    flags: int = 0,
    full_match: bool = False,
    image: Image | None = None,
    **function_kwargs: Any,
) -> RemoteFunction | _NoOpFunction:
    """Deploy a scorer that gives 1.0 when a column matches ``pattern``.

    ``full_match`` requires the pattern to cover the whole column instead of
    appearing anywhere in it. The matched text and any named groups are
    recorded as metadata, so a pattern can also pull a value out of the row.
    An unusable pattern is rejected here rather than on every row.
    """
    _assert_deployable("cc.scorers.regex_match")
    column = _validate_single(inputs, "regex_match")
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(
            f"regex_match: {pattern!r} is not a usable regular expression ({exc})"
        ) from exc

    def compare(**columns: Any) -> EvaluationScorerResult:
        text = _text(columns[column])
        found = compiled.fullmatch(text) if full_match else compiled.search(text)
        if found is None:
            return EvaluationScorerResult(
                score=0.0, metadata={"match": None, "groups": {}}
            )
        return EvaluationScorerResult(
            score=1.0, metadata={"match": found.group(0), "groups": found.groupdict()}
        )

    return _declare_scorer(
        name, (column,), compare, image=image, function_kwargs=function_kwargs
    )


_CONTAINS_MODES = ("all", "any", "fraction")


def _expected_items(value: Any) -> list[str]:
    """The reference column holds one substring, or a JSON array of them."""
    text = _text(value).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError:
            return [text]
        if isinstance(parsed, list):
            return [_text(item) for item in parsed]
    return [text] if text else []


def contains(
    *,
    name: str = "contains",
    inputs: Sequence[str] = ("output", "expected"),
    mode: str = "all",
    case_insensitive: bool = False,
    image: Image | None = None,
    **function_kwargs: Any,
) -> RemoteFunction | _NoOpFunction:
    """Deploy a scorer that looks for expected substrings in a column.

    The reference column holds one substring, or a JSON array of them. ``mode``
    scores them together: ``"all"`` demands every one, ``"any"`` one of them,
    and ``"fraction"`` gives the share that appeared. Which ones matched and
    which were missing are recorded as metadata.
    """
    _assert_deployable("cc.scorers.contains")
    candidate, reference = _validate_pair(inputs, "contains")
    if mode not in _CONTAINS_MODES:
        raise ValueError(
            f"contains: mode must be one of {_CONTAINS_MODES}, not {mode!r}"
        )

    def compare(**columns: Any) -> EvaluationScorerResult:
        haystack = _normalized(columns[candidate], case_insensitive=case_insensitive)
        wanted = [
            needle
            for needle in (
                _normalized(item, case_insensitive=case_insensitive)
                for item in _expected_items(columns[reference])
            )
            if needle
        ]
        matched = [needle for needle in wanted if needle in haystack]
        missing = [needle for needle in wanted if needle not in haystack]
        if not wanted:
            # Nothing was asked for, so everything asked for is present.
            score = 1.0 if mode == "all" else 0.0
        elif mode == "all":
            score = 1.0 if not missing else 0.0
        elif mode == "any":
            score = 1.0 if matched else 0.0
        else:
            score = len(matched) / len(wanted)
        return EvaluationScorerResult(
            score=score, metadata={"matched": matched, "missing": missing}
        )

    return _declare_scorer(
        name,
        (candidate, reference),
        compare,
        image=image,
        function_kwargs=function_kwargs,
    )


def _number(value: Any) -> float | None:
    try:
        parsed = float(_text(value).strip())
    except ValueError:
        return None
    # "inf" and "nan" parse as floats but cannot be scored: a relative
    # tolerance against infinity compares inf <= inf, and nan loses every
    # comparison, so both would read as a perfect or a zero score.
    return parsed if math.isfinite(parsed) else None


def numeric_close(
    *,
    name: str = "numeric-close",
    inputs: Sequence[str] = ("output", "expected"),
    tolerance: float | None = None,
    relative: bool = False,
    image: Image | None = None,
    **function_kwargs: Any,
) -> RemoteFunction | _NoOpFunction:
    """Deploy a scorer that compares two columns as numbers.

    With a ``tolerance`` the score is 1.0 or 0.0 depending on whether the two
    are within it, taken as a share of the larger value when ``relative``.
    Without one the score falls off with the gap between them, as
    ``1 - gap / max(abs(a), abs(b))``. The signed difference is recorded as
    metadata. A column that does not parse as a finite number scores 0.0,
    which covers ``inf`` and ``nan`` as well as ordinary text.
    """
    _assert_deployable("cc.scorers.numeric_close")
    candidate, reference = _validate_pair(inputs, "numeric_close")
    if tolerance is not None and not 0 <= tolerance < math.inf:
        raise ValueError(
            f"numeric_close: tolerance must be finite and not negative ({tolerance})"
        )

    def compare(**columns: Any) -> EvaluationScorerResult:
        a = _number(columns[candidate])
        b = _number(columns[reference])
        if a is None or b is None:
            return EvaluationScorerResult(
                score=0.0, metadata={"error": "not a finite number"}
            )
        gap = abs(a - b)
        largest = max(abs(a), abs(b))
        if tolerance is not None:
            allowed = tolerance * largest if relative else tolerance
            score = 1.0 if gap <= allowed else 0.0
        elif largest == 0:
            score = 1.0
        else:
            score = 1.0 - min(1.0, gap / largest)
        return EvaluationScorerResult(score=score, metadata={"difference": a - b})

    return _declare_scorer(
        name,
        (candidate, reference),
        compare,
        image=image,
        function_kwargs=function_kwargs,
    )


# rapidfuzz backs the fuzzy ratios; Jaccard and difflib need nothing extra.
_SIMILARITY_PACKAGES = ["rapidfuzz"]
_SIMILARITY_METHODS = (
    "jaro_winkler",
    "jaccard",
    "token_set",
    "token_sort",
    "partial",
    "sequence",
)


def _jaccard_similarity(a: str, b: str) -> float:
    """Character by character, as ``F.jaccard_similarity`` measures it."""
    left, right = set(a), set(b)
    union = left | right
    return 1.0 if not union else len(left & right) / len(union)


def _similarity(method: str, a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if method == "jaccard":
        return _jaccard_similarity(a, b)
    if method == "sequence":
        return difflib.SequenceMatcher(None, a, b).ratio()
    # Present in the scorer's image, never in the SDK's environment.
    from rapidfuzz import distance, fuzz

    if method == "jaro_winkler":
        return distance.JaroWinkler.normalized_similarity(a, b)
    ratio = {
        "token_set": fuzz.token_set_ratio,
        "token_sort": fuzz.token_sort_ratio,
        "partial": fuzz.partial_ratio,
    }[method]
    return ratio(a, b) / 100


def string_similarity(
    method: str,
    /,
    *,
    name: str | None = None,
    inputs: Sequence[str] = ("output", "expected"),
    case_insensitive: bool = False,
    image: Image | None = None,
    **function_kwargs: Any,
) -> RemoteFunction | _NoOpFunction:
    """Deploy a scorer that grades two columns by string similarity.

    ``method`` picks the measure: ``"jaro_winkler"``, ``"jaccard"``,
    ``"token_set"``, ``"token_sort"``, ``"partial"``, or ``"sequence"``. Each
    is the same quantity as the ``chalk.functions`` of that name, expressed as
    a similarity in ``[0, 1]``, and the raw value is recorded as metadata.
    Two empty columns score 1.0.
    """
    _assert_deployable("cc.scorers.string_similarity")
    if method not in _SIMILARITY_METHODS:
        raise ValueError(
            f"string_similarity: method must be one of {_SIMILARITY_METHODS}, "
            f"not {method!r}"
        )
    candidate, reference = _validate_pair(inputs, "string_similarity")

    def compare(**columns: Any) -> EvaluationScorerResult:
        a = _normalized(columns[candidate], case_insensitive=case_insensitive)
        b = _normalized(columns[reference], case_insensitive=case_insensitive)
        ratio = _similarity(method, a, b)
        return EvaluationScorerResult(
            score=ratio, metadata={"method": method, "ratio": ratio}
        )

    return _declare_scorer(
        name or sanitize_dns_label(method.replace("_", "-")),
        (candidate, reference),
        compare,
        image=image,
        function_kwargs=function_kwargs,
        packages=_SIMILARITY_PACKAGES,
    )


def _reject_constant(constant: str) -> Any:
    raise ValueError(f"{constant} is not valid JSON")


def _json_loads(text: str) -> Any:
    """Parse strict JSON. Python's parser otherwise takes NaN and Infinity,
    which no JSON document may contain."""
    return json.loads(text, parse_constant=_reject_constant)


def json_valid(
    *,
    name: str = "json-valid",
    inputs: Sequence[str] = ("output",),
    image: Image | None = None,
    **function_kwargs: Any,
) -> RemoteFunction | _NoOpFunction:
    """Deploy a scorer that gives 1.0 when a column parses as JSON.

    Useful on its own for a task asked to answer in JSON. The parser's
    complaint is recorded as metadata when it does not parse. ``NaN`` and
    ``Infinity`` are rejected, as the JSON grammar has no place for them.
    """
    _assert_deployable("cc.scorers.json_valid")
    column = _validate_single(inputs, "json_valid")

    def compare(**columns: Any) -> EvaluationScorerResult:
        try:
            _json_loads(_text(columns[column]))
        except ValueError as exc:
            return EvaluationScorerResult(score=0.0, metadata={"error": str(exc)})
        return EvaluationScorerResult(score=1.0, metadata={})

    return _declare_scorer(
        name, (column,), compare, image=image, function_kwargs=function_kwargs
    )


_MISSING = object()


def _at_path(document: Any, path: str) -> Any:
    """Walk a dotted path, taking digits as list indices."""
    current = document
    for step in path.split("."):
        if isinstance(current, Mapping) and step in current:
            current = current[step]
        elif isinstance(current, (list, tuple)) and step.lstrip("-").isdigit():
            index = int(step)
            if not -len(current) <= index < len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _json_equal(a: Any, b: Any) -> bool:
    """Compare as JSON does. Python holds ``True == 1``, so a boolean and a
    number would otherwise pass for each other at any depth."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return len(a) == len(b) and all(
            key in b and _json_equal(value, b[key]) for key, value in a.items()
        )
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(
            _json_equal(left, right) for left, right in zip(a, b)
        )
    return a == b


def _mismatched_keys(a: Any, b: Any) -> list[str]:
    """Top-level keys that differ, when both sides are objects."""
    if not isinstance(a, Mapping) or not isinstance(b, Mapping):
        return []
    return sorted(
        key
        for key in set(a) | set(b)
        if not _json_equal(a.get(key, _MISSING), b.get(key, _MISSING))
    )


def json_match(
    *,
    name: str = "json-match",
    inputs: Sequence[str] = ("output", "expected"),
    paths: Sequence[str] | None = None,
    image: Image | None = None,
    **function_kwargs: Any,
) -> RemoteFunction | _NoOpFunction:
    """Deploy a scorer that compares two columns as JSON documents.

    Without ``paths`` the whole documents must be equal, so object key order
    does not matter but list order does. With ``paths`` (dotted, as in
    ``"user.id"`` or ``"items.0.sku"``) the score is the share of those paths
    whose values agree. Either way the paths or keys that differed are recorded
    as metadata, and a column that does not parse scores 0.0. Values are
    compared as JSON, so ``true`` never passes for ``1``.
    """
    _assert_deployable("cc.scorers.json_match")
    candidate, reference = _validate_pair(inputs, "json_match")
    # A bare string would iterate into one path per character, and those
    # paths are absent on both sides, which reads as agreement.
    if isinstance(paths, str):
        raise TypeError(f"json_match: paths must be a sequence, not {paths!r}")
    wanted = tuple(paths) if paths is not None else None
    if wanted is not None and not wanted:
        raise ValueError("json_match: paths must name at least one path")

    def compare(**columns: Any) -> EvaluationScorerResult:
        parsed = []
        for column in (candidate, reference):
            try:
                parsed.append(_json_loads(_text(columns[column])))
            except ValueError as exc:
                return EvaluationScorerResult(
                    score=0.0, metadata={"error": f"{column}: {exc}"}
                )
        a, b = parsed
        if wanted is None:
            matched = _json_equal(a, b)
            return EvaluationScorerResult(
                score=1.0 if matched else 0.0,
                metadata={"mismatched": [] if matched else _mismatched_keys(a, b)},
            )
        mismatched = [
            path
            for path in wanted
            if not _json_equal(_at_path(a, path), _at_path(b, path))
        ]
        return EvaluationScorerResult(
            score=1 - len(mismatched) / len(wanted),
            metadata={"mismatched": mismatched},
        )

    return _declare_scorer(
        name,
        (candidate, reference),
        compare,
        image=image,
        function_kwargs=function_kwargs,
    )


_EMBEDDING_PACKAGES = ["openai"]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    magnitude = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    if magnitude == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / magnitude


def _embed(spec: _EmbeddingSpec, texts: Sequence[str]) -> list[Sequence[float]]:
    # Present in the scorer's image, never in the SDK's environment.
    from openai import OpenAI  # ty: ignore[unresolved-import]

    client = OpenAI(**_client_options(spec))
    embedded = client.embeddings.create(model=spec.model, input=list(texts))
    return [item.embedding for item in embedded.data]


def embedding_similarity(
    *,
    name: str = "embedding-similarity",
    inputs: Sequence[str] = ("output", "expected"),
    model: str = "text-embedding-3-small",
    base_url: str | None = None,
    api_key: Secret | None = None,
    image: Image | None = None,
    **function_kwargs: Any,
) -> RemoteFunction | _NoOpFunction:
    """Deploy a scorer that grades two columns by how close their meanings are.

    Both columns are embedded with ``model`` and scored by cosine similarity,
    clipped into ``[0, 1]``, with the raw cosine recorded as metadata. This
    catches an answer that is right but worded differently, which the string
    scorers mark wrong. ``base_url`` and ``api_key`` name the endpoint and the
    Secret holding its key, as they do for :func:`llm_judge`.
    """
    _assert_deployable("cc.scorers.embedding_similarity")
    candidate, reference = _validate_pair(inputs, "embedding_similarity")
    secrets: list[Secret] = []
    api_key_env = _DEFAULT_API_KEY_ENV
    if api_key is not None:
        api_key_env = _secret_env_var(api_key)
        secrets.append(api_key)
    spec = _EmbeddingSpec(model=model, base_url=base_url, api_key_env=api_key_env)

    def compare(**columns: Any) -> EvaluationScorerResult:
        a = _text(columns[candidate]).strip()
        b = _text(columns[reference]).strip()
        if not a and not b:
            return EvaluationScorerResult(score=1.0, metadata={"cosine": 1.0})
        if not a or not b:
            return EvaluationScorerResult(score=0.0, metadata={"cosine": 0.0})
        left, right = _embed(spec, (a, b))
        cosine = _cosine(left, right)
        return EvaluationScorerResult(
            score=max(0.0, min(1.0, cosine)), metadata={"cosine": cosine}
        )

    return _declare_scorer(
        name,
        (candidate, reference),
        compare,
        image=image,
        function_kwargs=function_kwargs,
        packages=_EMBEDDING_PACKAGES,
        secrets=secrets,
        extra_globals={"__chalk_spec__": spec},
    )
