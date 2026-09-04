"""Scorers declared as configuration and deployed as ordinary functions."""

from __future__ import annotations

import json
import keyword
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from chalkcompute._evaluation import EvaluationScorerResult
from chalkcompute._function import FunctionBuildError, RemoteFunction, _NoOpFunction
from chalkcompute._function import function as _function
from chalkcompute._image import Image
from chalkcompute._local_dev import should_strip_source
from chalkcompute._naming import sanitize_dns_label
from chalkcompute._secret import Secret

_DEFAULT_API_SERVER = "https://api.openai.com/v1"
_DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
_PYDANTIC_PACKAGE = "pydantic>=2"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Names the generated judge binds; an input column may not shadow them.
_RESERVED_NAMES = frozenset({"_judge", "_spec", "EvaluationScorerResult"})
_GENERATED_PREFIX = "_chalk_llm_judge_"
_REQUEST_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class _JudgeSpec:
    response_model: Any
    # The key the score is serialized under, which an alias can rename.
    score_key: str
    response_format: Mapping[str, Any]
    model: str
    instructions: str
    inputs: tuple[str, ...]
    prompt_fn: Callable[..., str] | None
    parse_fn: Callable[[str], Any] | None
    api_server: str | None
    api_key_env: str
    max_tokens: int | None
    temperature: float | None


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
    api_server: str | None = None,
    api_key: Secret | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    image: Image | None = None,
    **function_kwargs: Any,
) -> RemoteFunction | _NoOpFunction:
    """Deploy an LLM judge described by a pydantic model instead of a function body.

    ``response_model`` is a pydantic ``BaseModel`` class with a ``score`` field.
    The model is asked, via OpenAI structured outputs, for a JSON object matching
    it; ``score`` becomes the scorer's score and every other field is recorded
    as row metadata. A reply that fails validation fails the row.

    Parameters
    ----------
    response_model
        Pydantic model class describing the grade. Must declare ``score``.
    model
        Chat model name sent to the OpenAI-compatible endpoint.
    name
        Registered function name. Defaults to the model class name in kebab
        case (``TraceQuality`` deploys as ``trace-quality``).
    instructions
        System prompt.
    inputs
        Dataset columns the judge reads; ``output`` is the task's output. Each
        is rendered into the prompt and bound by name like any scorer parameter.
    prompt_fn
        ``prompt_fn(response_model, **inputs) -> str`` replacing the default
        prompt.
    parse_fn
        ``parse_fn(completion_text)`` returning a dict or ``response_model``
        instance, replacing ``json.loads``.
    api_server
        OpenAI-compatible base URL; ``/chat/completions`` is appended. Defaults
        to ``OPENAI_BASE_URL`` in the function environment, then OpenAI. Chalk's
        AI router works here; the function's environment id is sent with each
        request.
    api_key
        Secret exposing the API key to the function; its env var is read at
        call time. Defaults to ``OPENAI_API_KEY``.
    max_tokens, temperature
        Passed through to the completion request when set.
    image
        Base image; ``pydantic`` is installed on top of it.
    **function_kwargs
        Forwarded to :func:`chalkcompute.function`.
    """
    _assert_deployable()
    schema = _response_schema(response_model)
    inputs = _validate_inputs(inputs)
    name = name or _default_name(response_model)
    if prompt_fn is not None and not callable(prompt_fn):
        raise TypeError("prompt_fn must be callable")
    if parse_fn is not None and not callable(parse_fn):
        raise TypeError("parse_fn must be callable")

    secrets = list(function_kwargs.pop("secrets", None) or [])
    api_key_env = _DEFAULT_API_KEY_ENV
    if api_key is not None:
        api_key_env = _secret_env_var(api_key)
        secrets.append(api_key)

    score_key = _serialized_field_name(response_model, "score")
    spec = _JudgeSpec(
        response_model=response_model,
        score_key=score_key,
        response_format=_response_format(response_model, schema),
        model=model,
        instructions=instructions,
        inputs=inputs,
        prompt_fn=prompt_fn,
        parse_fn=parse_fn,
        api_server=api_server,
        api_key_env=api_key_env,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    # The judge is defined in the caller's module so the deploy ships that file,
    # where the response model lives, and the worker shim imports it back.
    frame = sys._getframe(1)
    caller_module = frame.f_globals.get("__name__", "__main__")
    py_name = _GENERATED_PREFIX + re.sub(r"[^0-9A-Za-z_]", "_", name)
    params = ", ".join(f"{column}: str" for column in inputs)
    columns = ", ".join(f"{column!r}: {column}" for column in inputs)
    source = (
        f"def {py_name}({params}) -> EvaluationScorerResult:\n"
        f"    return _judge(_spec, {{{columns}}})\n"
    )
    namespace: dict[str, Any] = {
        "__name__": caller_module,
        "EvaluationScorerResult": EvaluationScorerResult,
        "_judge": _judge,
        "_spec": spec,
    }
    exec(compile(source, frame.f_code.co_filename, "exec"), namespace)
    judge = namespace[py_name]
    judge.__chalk_scorer_response_schema__ = json.dumps(
        _metadata_schema(response_model, score_key)
    )

    base_image = image if image is not None else Image.debian_slim()
    deployed = _function(
        name=name,
        image=base_image.pip_install([_PYDANTIC_PACKAGE]),
        secrets=secrets or None,
        **function_kwargs,
    )(judge)
    module = sys.modules.get(caller_module)
    if module is not None:
        setattr(module, py_name, deployed)
    return deployed


def _assert_deployable() -> None:
    """Strip mode removes the caller's chalkcompute import but keeps its
    ``llm_judge(...)`` call, so the shipped module would fail to import in the
    pod before scoring anything."""
    if should_strip_source():
        raise FunctionBuildError(
            "cc.scorers.llm_judge cannot be deployed in strip mode "
            "(CHALK_FUNCTION_STRIP_SOURCE, or a 0.0.0 source checkout): the "
            "judge is generated by chalkcompute at import time, and stripping "
            "removes the chalkcompute import the deployed module needs. Deploy "
            "from an environment with a published chalkcompute installed, or "
            "set CHALK_FUNCTION_LOCAL_SDK_COPY=1 with "
            "CHALK_FUNCTION_STRIP_SOURCE=0 to mount the local SDK."
        )


def _validate_inputs(inputs: Sequence[str]) -> tuple[str, ...]:
    """Column names must be usable as parameters of the generated judge."""
    if isinstance(inputs, str):
        raise TypeError(
            f"inputs must be a sequence of column names, not the string {inputs!r}"
        )
    columns = tuple(inputs)
    if not columns:
        raise ValueError("inputs must name at least one column")
    for column in columns:
        if not isinstance(column, str) or not _IDENTIFIER_RE.match(column):
            raise ValueError(f"input {column!r} is not a Python identifier")
        if keyword.iskeyword(column):
            raise ValueError(f"input {column!r} is a Python keyword")
        if column in _RESERVED_NAMES or column.startswith(_GENERATED_PREFIX):
            raise ValueError(f"input {column!r} is reserved by the generated judge")
    duplicates = sorted({c for c in columns if columns.count(c) > 1})
    if duplicates:
        raise ValueError(f"duplicate inputs: {', '.join(duplicates)}")
    return columns


def _default_name(response_model: Any) -> str:
    """``TraceQuality`` -> ``trace-quality``."""
    spaced = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])", "-", getattr(response_model, "__name__", "")
    )
    return sanitize_dns_label(spaced)


def _response_schema(response_model: Any) -> dict[str, Any]:
    if not isinstance(response_model, type) or not hasattr(
        response_model, "model_json_schema"
    ):
        raise TypeError("response_model must be a pydantic BaseModel class")
    if "score" not in getattr(response_model, "model_fields", {}):
        raise ValueError(
            "response_model must declare a `score` field; it becomes the scorer's "
            "score and the remaining fields are recorded as metadata"
        )
    schema = response_model.model_json_schema()
    _assert_closed_objects(schema, path="response_model")
    return schema


def _serialized_field_name(response_model: Any, field: str) -> str:
    """The key ``model_dump(by_alias=True)`` writes ``field`` under."""
    info = response_model.model_fields[field]
    return info.serialization_alias or info.alias or field


def _metadata_schema(response_model: Any, score_key: str) -> dict[str, Any]:
    """The schema of what lands in row metadata: the model as it serializes,
    minus the score, which travels in its own column. Named the way the rows
    are, so the server can find every declared field it reads about."""
    schema = dict(response_model.model_json_schema(mode="serialization"))
    properties = dict(schema.get("properties", {}))
    properties.pop(score_key, None)
    schema["properties"] = properties
    if "required" in schema:
        schema["required"] = [name for name in schema["required"] if name != score_key]
    return schema


def _assert_closed_objects(schema: Any, *, path: str) -> None:
    """Strict structured outputs reject an object without fixed properties.

    A mapping field (``dict[str, str]``) becomes ``{"type": "object",
    "additionalProperties": {...}}``, which cannot be closed with
    ``additionalProperties: false``, so OpenAI rejects the whole schema.
    """
    if isinstance(schema, list):
        for index, item in enumerate(schema):
            _assert_closed_objects(item, path=f"{path}[{index}]")
        return
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object" and "properties" not in schema:
        raise ValueError(
            f"response_model field {path} is a mapping, which OpenAI's strict "
            "structured outputs cannot express (every object must declare its "
            "properties). Use a nested model with named fields, or a JSON string."
        )
    for key, value in schema.items():
        if key in ("properties", "$defs"):
            for field_name, field_schema in (value or {}).items():
                _assert_closed_objects(field_schema, path=f"{path}.{field_name}")
        elif key in ("items", "anyOf", "allOf", "oneOf", "prefixItems"):
            _assert_closed_objects(value, path=path)


def _response_format(response_model: type, schema: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": response_model.__name__,
            "schema": _strict(schema),
            "strict": True,
        },
    }


def _strict(schema: Any) -> Any:
    """OpenAI strict mode: every object closed, every property required, and
    no ``default`` -- an optional field emits ``default: null``, which strict
    mode rejects."""
    if isinstance(schema, dict):
        out = {key: _strict(value) for key, value in schema.items()}
        if out.get("default", ...) is None:
            del out["default"]
        if out.get("type") == "object" and isinstance(out.get("properties"), dict):
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out
    if isinstance(schema, list):
        return [_strict(value) for value in schema]
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
    body: dict[str, Any] = {
        "model": spec.model,
        "messages": messages,
        "response_format": dict(spec.response_format),
    }
    if spec.max_tokens is not None:
        body["max_tokens"] = spec.max_tokens
    if spec.temperature is not None:
        body["temperature"] = spec.temperature

    api_key = os.environ.get(spec.api_key_env)
    if not api_key:
        raise RuntimeError(
            f"llm_judge: {spec.api_key_env} is not set in the function environment; "
            "pass api_key=Secret.from_chalk_env(...) to llm_judge"
        )
    api_server = (
        spec.api_server or os.environ.get("OPENAI_BASE_URL") or _DEFAULT_API_SERVER
    ).rstrip("/")
    response = _post_json(f"{api_server}/chat/completions", body, api_key)
    text = response["choices"][0]["message"]["content"]

    graded = spec.parse_fn(text) if spec.parse_fn is not None else json.loads(text)
    if not isinstance(graded, spec.response_model):
        graded = spec.response_model.model_validate(graded)
    # Aliases: serialize the way the stamped schema is named, so the server
    # finds each field under the key it read about.
    data = graded.model_dump(mode="json", by_alias=True)
    score = data.pop(spec.score_key)
    return EvaluationScorerResult(score=float(score), metadata=data or None)


def _default_prompt(spec: _JudgeSpec, columns: Mapping[str, Any]) -> str:
    schema = spec.response_model.model_json_schema()
    lines = [
        "Grade the material below. Respond with a JSON object matching the "
        "response schema.",
        "",
        "Fields:",
    ]
    for field_name, prop in schema.get("properties", {}).items():
        lines.append(_describe_field(field_name, prop))
    for column, value in columns.items():
        lines.extend(["", f"## {column}", str(value)])
    return "\n".join(lines)


def _describe_field(name: str, prop: Mapping[str, Any]) -> str:
    parts = [str(prop.get("type", "value"))]
    if "minimum" in prop and "maximum" in prop:
        parts.append(f"{prop['minimum']} to {prop['maximum']}")
    elif "minimum" in prop:
        parts.append(f"at least {prop['minimum']}")
    elif "maximum" in prop:
        parts.append(f"at most {prop['maximum']}")
    if "enum" in prop:
        parts.append("one of " + ", ".join(str(v) for v in prop["enum"]))
    line = f"- {name} ({', '.join(parts)})"
    description = prop.get("description")
    return f"{line}: {description}" if description else line


def _post_json(url: str, body: Mapping[str, Any], api_key: str) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    # Chalk's AI router resolves the calling environment from this header, as
    # F.openai_complete does; other endpoints ignore it.
    env_id = os.environ.get("CHALK_ENVIRONMENT_ID") or os.environ.get(
        "CHALK_ACTIVE_ENVIRONMENT"
    )
    if env_id:
        headers["X-Chalk-Env-Id"] = env_id
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:500].decode("utf-8", "replace")
        raise RuntimeError(f"llm_judge: {url} returned {exc.code}: {detail}") from exc
