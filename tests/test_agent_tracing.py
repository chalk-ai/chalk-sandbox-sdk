import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

import chalkcompute._tracing as _tracing
from chalkcompute import AgentResult, AgentTask, agent_trace, trace_headers


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(_tracing, "_ensure_user_tracing_configured", lambda: None)
    monkeypatch.setattr(_tracing, "get_tracer", lambda: provider.get_tracer("test"))
    return exporter


def test_agent_trace_records_task_turn_and_result(spans: InMemorySpanExporter) -> None:
    task = AgentTask(
        kind="account_login",
        goal="Log in to the account",
        metadata={"site": "example.com"},
    )

    with agent_trace("Account login", task=task) as trace:
        with trace.turn(
            index=0,
            observation={"url": "https://example.com/login"},
        ) as turn:
            turn.set_action({"kind": "click", "ref": "e1"})
        trace.set_result(
            AgentResult(
                success=True,
                status="complete",
                steps=1,
                summary="Logged in",
            )
        )

    by_name = {span.name: span for span in spans.get_finished_spans()}
    agent = by_name["Account login"]
    turn = by_name["agent.turn"]

    assert agent.attributes["openinference.span.kind"] == "AGENT"
    assert agent.attributes["chalk.agent.task.kind"] == "account_login"
    assert agent.attributes["chalk.agent.result.success"] is True
    assert agent.attributes["chalk.agent.result.status"] == "complete"
    assert agent.attributes["chalk.agent.result.steps"] == 1
    assert agent.attributes["input.mime_type"] == "application/json"
    assert json.loads(agent.attributes["input.value"]) == {
        "kind": "account_login",
        "goal": "Log in to the account",
        "metadata": {"site": "example.com"},
    }
    assert json.loads(agent.attributes["output.value"]) == {
        "success": True,
        "status": "complete",
        "steps": 1,
        "summary": "Logged in",
    }
    assert turn.attributes["openinference.span.kind"] == "CHAIN"
    assert turn.attributes["chalk.agent.turn.index"] == 0
    assert turn.attributes["chalk.agent.action.kind"] == "click"
    assert json.loads(turn.attributes["input.value"]) == {
        "url": "https://example.com/login"
    }
    assert json.loads(turn.attributes["output.value"]) == {
        "kind": "click",
        "ref": "e1",
    }
    assert turn.parent.span_id == agent.context.span_id


def test_trace_headers_propagate_active_agent_trace(
    spans: InMemorySpanExporter,
) -> None:
    with agent_trace(
        "Account login",
        task=AgentTask(kind="account_login", goal="Log in"),
    ):
        headers = trace_headers()

    assert headers["traceparent"].startswith("00-")


def test_agent_task_requires_kind() -> None:
    with pytest.raises(ValueError, match="task kind cannot be empty"):
        AgentTask(kind="", goal="Log in")


def test_agent_result_rejects_negative_steps() -> None:
    with pytest.raises(ValueError, match="result steps cannot be negative"):
        AgentResult(success=False, status="failed", steps=-1)
