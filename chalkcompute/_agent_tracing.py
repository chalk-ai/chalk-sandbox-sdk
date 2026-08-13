from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from chalkcompute._tracing import span

_SPAN_KIND = "openinference.span.kind"
_JSON_MIME_TYPE = "application/json"
_ATTRIBUTE_PREFIX = "chalk.agent"


@dataclass(frozen=True)
class AgentTask:
    kind: str
    goal: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("task kind cannot be empty")
        if not self.goal.strip():
            raise ValueError("task goal cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"kind": self.kind, "goal": self.goal}
        if self.metadata:
            value["metadata"] = dict(self.metadata)
        return value


@dataclass(frozen=True)
class AgentResult:
    success: bool
    status: str
    steps: int
    summary: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.status.strip():
            raise ValueError("result status cannot be empty")
        if self.steps < 0:
            raise ValueError("result steps cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "success": self.success,
            "status": self.status,
            "steps": self.steps,
            "summary": self.summary,
        }
        if self.metadata:
            value["metadata"] = dict(self.metadata)
        return value


def _set_json(span_instance: Any, prefix: str, value: Any) -> None:
    if span_instance is None:
        return
    span_instance.set_attribute(f"{prefix}.value", json.dumps(value))
    span_instance.set_attribute(f"{prefix}.mime_type", _JSON_MIME_TYPE)


class AgentTurn:
    def __init__(self, span_instance: Any) -> None:
        self._span = span_instance

    def set_action(self, action: Mapping[str, Any]) -> None:
        action_value = dict(action)
        _set_json(self._span, "output", action_value)
        action_kind = action_value.get("kind")
        if self._span is not None and isinstance(action_kind, str):
            self._span.set_attribute(f"{_ATTRIBUTE_PREFIX}.action.kind", action_kind)


class AgentTrace:
    def __init__(self, span_instance: Any) -> None:
        self._span = span_instance

    @contextlib.contextmanager
    def turn(self, *, index: int, observation: Any) -> Iterator[AgentTurn]:
        if index < 0:
            raise ValueError("turn index cannot be negative")
        with span("agent.turn") as turn_span:
            if turn_span is not None:
                turn_span.set_attribute(_SPAN_KIND, "CHAIN")
                turn_span.set_attribute(f"{_ATTRIBUTE_PREFIX}.turn.index", index)
            _set_json(turn_span, "input", observation)
            yield AgentTurn(turn_span)

    def set_result(self, result: AgentResult) -> None:
        _set_json(self._span, "output", result.to_dict())
        if self._span is not None:
            self._span.set_attribute(
                f"{_ATTRIBUTE_PREFIX}.result.success", result.success
            )
            self._span.set_attribute(
                f"{_ATTRIBUTE_PREFIX}.result.status", result.status
            )
            self._span.set_attribute(f"{_ATTRIBUTE_PREFIX}.result.steps", result.steps)


@contextlib.contextmanager
def agent_trace(name: str, *, task: AgentTask) -> Iterator[AgentTrace]:
    if not name.strip():
        raise ValueError("agent trace name cannot be empty")
    with span(name) as agent_span:
        if agent_span is not None:
            agent_span.set_attribute(_SPAN_KIND, "AGENT")
            agent_span.set_attribute(f"{_ATTRIBUTE_PREFIX}.task.kind", task.kind)
        _set_json(agent_span, "input", task.to_dict())
        yield AgentTrace(agent_span)
