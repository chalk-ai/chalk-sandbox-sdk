"""Replay a finished Harbor trial as OpenInference spans.

Harbor emits no telemetry of its own, and the agent runs inside the trial's sandbox, so
nothing it does reaches the tracer of the process that launched the trial. What Harbor
does keep is a record: ``result.json`` (phase timings, rewards, exceptions) and, for agents
that support it, ``agent/trajectory.json`` in ATIF (every model turn and tool call, with
timestamps, token counts, and cost). This module turns that record into spans after the
fact, with the original timestamps, under whatever span is current. Called from a Chalk
evaluation task, that is the row's session, so the row's trace shows the trial.

Span tree::

    harbor.trial                      AGENT  instruction in, final message out, reward
    ├── harbor.environment_setup      CHAIN
    ├── harbor.agent_setup            CHAIN
    ├── harbor.agent_execution        CHAIN
    │   ├── <model>                   LLM    one per agent turn: message, tokens, cost
    │   └── <tool name>               TOOL   one per tool call: arguments in, observation out
    └── harbor.verifier               CHAIN  rewards

A turn's LLM span runs from the previous step to the turn's own timestamp (the time the
model took to produce it); its tool spans run from that timestamp to the next step's (the
time the tools took before the agent continued). ATIF records no finer timing than that.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

_TRACER_NAME = "chalk_harbor"
# Attribute values are capped so one huge observation cannot blow the span size limit.
_MAX_VALUE_CHARS = 16_000
_PHASES = ("environment_setup", "agent_setup", "agent_execution", "verifier")


def emit_trial_spans(trial_dir: Path | str, *, instruction: str | None = None) -> None:
    """Emit spans for the Harbor trial recorded in ``trial_dir`` under the current span."""
    trial_dir = Path(trial_dir)
    result = json.loads((trial_dir / "result.json").read_text())
    trajectory_path = trial_dir / "agent" / "trajectory.json"
    trajectory = (
        json.loads(trajectory_path.read_text()) if trajectory_path.exists() else None
    )
    tracer = trace.get_tracer(_TRACER_NAME)

    started, finished = _ns(result.get("started_at")), _ns(result.get("finished_at"))
    root = tracer.start_span("harbor.trial", start_time=started)
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    _set(
        root,
        {
            "openinference.span.kind": "AGENT",
            "harbor.task": result.get("task_name"),
            "harbor.trial": result.get("trial_name"),
            "harbor.agent": ((result.get("agent_info") or {}).get("name")),
            "harbor.reward": rewards.get("reward"),
            "input.value": instruction,
            "output.value": _final_message(trajectory),
        },
    )
    exception = result.get("exception_info")
    if exception:
        root.set_status(Status(StatusCode.ERROR, exception.get("exception_type")))
        _set(root, {"harbor.exception": exception.get("exception_message")})

    parent = trace.set_span_in_context(root)
    for phase in _PHASES:
        block = result.get(phase)
        if not block or not block.get("started_at"):
            continue
        span = tracer.start_span(
            f"harbor.{phase}", context=parent, start_time=_ns(block["started_at"])
        )
        _set(span, {"openinference.span.kind": "CHAIN"})
        if phase == "verifier":
            _set(
                span,
                {f"harbor.reward.{name}": value for name, value in rewards.items()},
            )
        if phase == "agent_execution" and trajectory is not None:
            _emit_trajectory(
                tracer, span, trajectory, end=_ns(block.get("finished_at"))
            )
        span.end(end_time=_ns(block.get("finished_at")))
    root.end(end_time=finished)


def _emit_trajectory(
    tracer: trace.Tracer, parent_span: Span, trajectory: dict[str, Any], end: int | None
) -> None:
    parent = trace.set_span_in_context(parent_span)
    agent = trajectory.get("agent") or {}
    steps = trajectory.get("steps") or []
    times = [_ns(step.get("timestamp")) for step in steps]
    for index, step in enumerate(steps):
        if step.get("source") != "agent":
            continue
        previous = next((t for t in reversed(times[:index]) if t), times[index])
        following = next((t for t in times[index + 1 :] if t), end)
        metrics = step.get("metrics") or {}
        model = step.get("model_name") or agent.get("model_name") or "llm"
        llm = tracer.start_span(model, context=parent, start_time=previous)
        _set(
            llm,
            {
                "openinference.span.kind": "LLM",
                "llm.model_name": model,
                "llm.provider": model.split("/", 1)[0] if "/" in model else None,
                "llm.token_count.prompt": metrics.get("prompt_tokens"),
                "llm.token_count.completion": metrics.get("completion_tokens"),
                "llm.token_count.prompt_details.cache_read": metrics.get(
                    "cached_tokens"
                ),
                "llm.token_count.total": _sum(
                    metrics.get("prompt_tokens"), metrics.get("completion_tokens")
                ),
                "llm.cost.total": metrics.get("cost_usd"),
                "output.value": step.get("message") or None,
                "harbor.reasoning": step.get("reasoning_content"),
            },
        )
        llm.end(end_time=times[index] or previous)

        observations = {
            result.get("source_call_id"): result.get("content")
            for result in ((step.get("observation") or {}).get("results") or [])
        }
        for call in step.get("tool_calls") or []:
            tool = tracer.start_span(
                call.get("function_name") or "tool",
                context=parent,
                start_time=times[index],
            )
            _set(
                tool,
                {
                    "openinference.span.kind": "TOOL",
                    "tool.name": call.get("function_name"),
                    "tool_call.id": call.get("tool_call_id"),
                    "input.value": json.dumps(call.get("arguments"), default=str),
                    "input.mime_type": "application/json",
                    "output.value": _text(observations.get(call.get("tool_call_id"))),
                },
            )
            tool.end(end_time=following)


def _final_message(trajectory: dict[str, Any] | None) -> str | None:
    if trajectory is None:
        return None
    for step in reversed(trajectory.get("steps") or []):
        if step.get("source") == "agent" and step.get("message"):
            return step["message"]
    return None


def _set(span: Span, attributes: dict[str, Any]) -> None:
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, str):
            value = value[:_MAX_VALUE_CHARS]
        elif not isinstance(value, (bool, int, float)):
            value = json.dumps(value, default=str)[:_MAX_VALUE_CHARS]
        span.set_attribute(key, value)


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _sum(*values: int | None) -> int | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _ns(timestamp: str | None) -> int | None:
    if not timestamp:
        return None
    return int(datetime.fromisoformat(timestamp).timestamp() * 1e9)


__all__ = ["emit_trial_spans"]
