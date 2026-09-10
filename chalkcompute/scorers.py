"""Scorers you declare instead of write."""

from __future__ import annotations

import json
import keyword
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from chalkcompute._evaluation import EvaluationScorerResult
from chalkcompute._evaluation import SqlScorer as SqlScorer
from chalkcompute._function import FunctionBuildError, RemoteFunction, _NoOpFunction
from chalkcompute._function import function as _function
from chalkcompute._image import Image
from chalkcompute._local_dev import should_strip_source
from chalkcompute._naming import sanitize_dns_label
from chalkcompute._secret import Secret

_DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
# In the judge's image, not the SDK's. Unbounded, so a user's own image keeps
# the versions it has.
_JUDGE_PACKAGES = ["pydantic", "openai"]
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    completion_kwargs: dict[str, Any]


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
    ``prompt_fn(response_model, **inputs)`` replaces the prompt; ``parse_fn``
    replaces structured outputs. ``base_url`` takes any OpenAI-compatible URL
    and ``api_key`` a Secret read in the pod. ``completion_kwargs`` (say
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

    The generated function is defined in the caller's module so the deploy
    ships that file, where anything the body closes over lives, and the
    pod-side shim imports it back. ``stamp`` sets attributes on it and
    ``extra_globals`` names values in its module namespace.
    """
    frame = sys._getframe(frame_depth)
    caller_module = frame.f_globals.get("__name__", "__main__")
    py_name = prefix + re.sub(r"[^0-9A-Za-z_]", "_", name)
    params = ", ".join(f"{column}: str" for column in inputs)
    columns = ", ".join(f"{column}={column}" for column in inputs)
    source = (
        f"def {py_name}({params}) -> __chalk_result__:\n"
        f"    return __chalk_body__({columns})\n"
    )
    namespace: dict[str, Any] = {
        "__name__": caller_module,
        "__chalk_result__": EvaluationScorerResult,
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
        parts.extend([f"## {column}", str(value)])
    return "\n\n".join(parts)


def _client_options(spec: _JudgeSpec) -> dict[str, Any]:
    api_key = os.environ.get(spec.api_key_env)
    if not api_key:
        raise RuntimeError(
            f"llm_judge: {spec.api_key_env} is not set in the function environment; "
            "pass api_key=Secret.from_chalk_env(...) to llm_judge"
        )
    options: dict[str, Any] = {"api_key": api_key}
    base_url = spec.base_url or os.environ.get("OPENAI_BASE_URL")
    if base_url:
        options["base_url"] = base_url
    # Chalk's AI router routes on this; other endpoints ignore it.
    env_id = os.environ.get("CHALK_ENVIRONMENT_ID") or os.environ.get(
        "CHALK_ACTIVE_ENVIRONMENT"
    )
    if env_id:
        options["default_headers"] = {"X-Chalk-Env-Id": env_id}
    return options


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
# Deterministic scorers
# ---------------------------------------------------------------------------


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
