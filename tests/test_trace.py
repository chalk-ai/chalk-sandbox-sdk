from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
import json
from typing import Any

import pyarrow as pa
import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError

from chalkcompute._arrow_schema import rich_to_arrow_schema
from chalkcompute._arrow_serde import (
    arrow_decoder_for_annotation,
    arrow_encoder_for_annotation,
)
from chalkcompute._trace import (
    SpanKind,
    SpanStatusCode,
    Trace,
    TraceClient,
    TraceError,
    TraceNotFoundError,
)
from chalkcompute._gen.chalk.server.v1 import trace_pb
from tests._fakes import FakeMethod, StubbedConnectClient


def _response(payload: Mapping[str, Any]) -> Any:
    if "spans" in payload:
        message_type = trace_pb.ListSpanResponse
    elif "sessions" in payload:
        message_type = trace_pb.SearchSessionsResponse
    elif "traceSummaries" in payload:
        message_type = trace_pb.SearchTraceSummariesResponse
    else:
        raise AssertionError(f"unknown trace response fixture: {payload}")
    return message_type.from_json(json.dumps(payload))


class _FakeConnectClient(StubbedConnectClient):
    def __init__(self, responses: Mapping[str, list[Any]]) -> None:
        self._responses = {method: list(values) for method, values in responses.items()}
        self.calls: list[tuple[str, Any]] = []

    def call(self, method: FakeMethod, request: Any, **_kwargs: Any) -> Any:
        self.calls.append((method.name, request))
        try:
            outcome = self._responses[method.name].pop(0)
        except (KeyError, IndexError) as exc:
            raise AssertionError(f"unexpected RPC call: {method.name}") from exc
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _span_dict(span_id: str, **attributes: str) -> dict[str, Any]:
    return {
        "spanId": span_id,
        "traceId": "trace-1",
        "operationName": "llm.completion",
        "startTime": "2026-08-17T00:00:00Z",
        "endTime": "2026-08-17T00:00:01Z",
        "duration": "1.500s",
        "status": {"code": "CHALK_STATUS_CODE_OK"},
        "kind": "CHALK_SPAN_KIND_CLIENT",
        "attributes": {"session.id": "session-1", **attributes},
        "resourceAttributes": {"service.name": "agent"},
    }


def _client(responses: Mapping[str, list[Any]]) -> tuple[TraceClient, Any]:
    connect = _FakeConnectClient(responses)
    return TraceClient(connect_client=connect), connect


# --- Arrow contract -------------------------------------------------------


def test_trace_declares_the_same_arrow_type_as_str() -> None:
    """A scorer may annotate `trace: Trace` or `trace: str` interchangeably."""
    typed = rich_to_arrow_schema({"trace": Trace})
    untyped = rich_to_arrow_schema({"trace": str})
    assert typed.columns[0].arrow_type == untyped.columns[0].arrow_type
    assert typed.columns[0].arrow_type.arrow_type_enum.field == "large_utf8"


def test_trace_roundtrips_through_the_arrow_scalar_contract() -> None:
    encode = arrow_encoder_for_annotation(Trace)
    decode = arrow_decoder_for_annotation(Trace)
    assert encode is not None and decode is not None
    scalar = encode(Trace("session-1"))
    assert scalar.type == pa.large_utf8()
    assert decode(scalar) == Trace("session-1")


def test_optional_trace_is_nullable_and_still_decodes_to_a_handle() -> None:
    """Chalk binds `trace` to null for a task that emitted no telemetry."""
    schema = rich_to_arrow_schema({"trace": Trace | None})
    field = schema.columns[0]
    assert field.nullable is True
    assert field.arrow_type.arrow_type_enum.field == "large_utf8"

    decode = arrow_decoder_for_annotation(Trace | None)
    assert decode is not None
    assert decode(pa.scalar("session-1", type=pa.large_utf8())) == Trace("session-1")


def test_required_trace_is_not_nullable() -> None:
    assert rich_to_arrow_schema({"trace": Trace}).columns[0].nullable is False


@pytest.mark.parametrize("value", ["", "   "])
def test_trace_rejects_a_blank_id(value: str) -> None:
    with pytest.raises(ValueError, match="id is required"):
        Trace(value)


def test_trace_strips_surrounding_whitespace() -> None:
    assert Trace(" session-1 ").id == "session-1"


# --- Transcript -----------------------------------------------------------


def _turn(span_id: str, start: str, **attributes: str) -> dict[str, Any]:
    return {**_span_dict(span_id, **attributes), "startTime": start, "endTime": start}


def test_transcript_renders_llm_turns_oldest_first() -> None:
    """Only spans carrying a prompt are turns; they render in start order."""
    client, connect = _client(
        {
            "list_span": [
                _response(
                    {
                        "spans": [
                            _turn(
                                "span-2",
                                "2026-08-17T00:00:05Z",
                                **{
                                    "input.value": "second prompt",
                                    "output.value": "second answer",
                                    "llm.model_name": "claude-sonnet-5",
                                    "llm.token_count.total": "1234",
                                },
                            ),
                            _turn(
                                "span-1",
                                "2026-08-17T00:00:01Z",
                                **{
                                    "input.value": "first prompt",
                                    "output.value": "first answer",
                                },
                            ),
                            _turn(
                                "span-3",
                                "2026-08-17T00:00:02Z",
                                **{"db.statement": "select 1"},
                            ),
                        ]
                    }
                )
            ]
        }
    )
    text = Trace("session-1", _client=client).transcript(wait=False)

    _, request = connect.calls[0]
    assert request.query == 'session:"session-1"'
    assert text == (
        "### turn 1 (unknown model)\n[prompt]\nfirst prompt\n[completion]\nfirst answer"
        "\n\n"
        "### turn 2 (claude-sonnet-5, 1,234 tokens)\n[prompt]\nsecond prompt\n[completion]\nsecond answer"
    )


def test_transcript_keeps_the_prompt_tail_and_the_completion_head() -> None:
    """The newest context the model saw, and the start of what it said, survive."""
    client, _ = _client(
        {
            "list_span": [
                _response(
                    {
                        "spans": [
                            _turn(
                                "span-1",
                                "2026-08-17T00:00:01Z",
                                **{
                                    "input.value": "p" * 10 + "END",
                                    "output.value": "START" + "c" * 10,
                                },
                            )
                        ]
                    }
                )
            ]
        }
    )
    text = Trace("session-1", _client=client).transcript(
        wait=False, max_chars_per_turn=5
    )

    assert "[prompt]\n… [8 earlier characters]\nppEND" in text
    assert "[completion]\nSTART\n… [10 more characters]" in text


def test_transcript_of_a_session_with_no_llm_turns_says_so() -> None:
    client, _ = _client({"list_span": [_response({"spans": []})]})
    text = Trace("session-1", _client=client).transcript(wait=False)
    assert text == "(no LLM turns were recorded in this session)"


def test_transcript_rejects_a_non_positive_turn_budget() -> None:
    client, _ = _client({"list_span": [_response({"spans": []})]})
    with pytest.raises(ValueError, match="max_chars_per_turn"):
        Trace("session-1", _client=client).transcript(wait=False, max_chars_per_turn=0)


@pytest.mark.parametrize("include_inline_turn", [False, True])
def test_transcript_keeps_externalized_turns(
    clock: list[float], include_inline_turn: bool
) -> None:
    """A body stored outside the span is present, even before it can be loaded."""
    spans = [
        _turn(
            "large",
            "2026-08-17T00:00:02Z",
            **{
                "chalk.trace.input.content_uri": "s3://private-test-bucket/prompt",
                "chalk.trace.output.content_uri": "s3://private-test-bucket/completion",
                "chalk.trace.input.content_bytes": "65537",
                "chalk.trace.output.content_bytes": "65537",
            },
        )
    ]
    if include_inline_turn:
        spans.append(
            _turn("small", "2026-08-17T00:00:01Z", **{"input.value": "small prompt"})
        )
    response = _response({"spans": spans})
    client, connect = _client({"list_span": [response] * 40})

    text = Trace("session-1", _client=client).transcript(quiet_for=30)

    assert text.count("### turn ") == len(spans)
    assert "[prompt]\n(content stored externally; not loaded)" in text
    assert "[completion]\n(content stored externally; not loaded)" in text
    assert "s3://" not in text
    assert "no LLM turns" not in text
    assert clock[0] == 30
    assert len(connect.calls) == 16


def test_transcript_marks_externalized_output_for_inline_prompt() -> None:
    client, _ = _client(
        {
            "list_span": [
                _response(
                    {
                        "spans": [
                            _span_dict(
                                "large-output",
                                **{
                                    "input.value": "small prompt",
                                    "chalk.trace.output.content_uri": "s3://private-test-bucket/completion",
                                },
                            )
                        ]
                    }
                )
            ]
        }
    )

    text = Trace("session-1", _client=client).transcript(wait=False)

    assert "small prompt" in text
    assert "[completion]\n(content stored externally; not loaded)" in text
    assert "s3://" not in text


@pytest.mark.parametrize("direction", ["input", "output"])
@pytest.mark.parametrize(
    ("metadata", "note"),
    [
        ({"content_uri": ""}, "content stored externally; not loaded"),
        ({"content_uri": "not a URI"}, "content stored externally; not loaded"),
        ({"content_bytes": "65537"}, "body not recorded inline"),
        ({"upload_omitted": "upload_failed"}, "upload omitted"),
        ({"upload_omitted": "upload_timed_out"}, "upload omitted"),
        (
            {"truncation": '{"raw_content":{"reason":"serialization_failed"}}'},
            "truncated during recording",
        ),
        ({"truncation": "malformed metadata"}, "truncated during recording"),
    ],
)
def test_transcript_surfaces_missing_content_on_either_side(
    direction: str, metadata: dict[str, str], note: str
) -> None:
    attributes = {
        f"chalk.trace.{direction}.{key}": value for key, value in metadata.items()
    }
    if direction == "output":
        attributes["input.value"] = "prompt"
    response = _response({"spans": [_span_dict("span-1", **attributes)]})
    client, connect = _client({"list_span": [response, response]})
    trace = Trace("session-1", _client=client)

    # A tiny text budget must not hide the diagnostic, and no content URI is
    # dereferenced: the only RPCs are the two explicitly requested span reads.
    text = trace.transcript(wait=False, max_chars_per_turn=1)
    assert "### turn 1" in text
    assert note in text
    with pytest.raises(TraceError, match=f"incomplete: {direction}"):
        trace.transcript(wait=False, require_content=True)
    assert [method for method, _ in connect.calls] == ["list_span", "list_span"]


@pytest.mark.parametrize("direction", ["input", "output"])
@pytest.mark.parametrize(
    "truncation", ['{"messages":{"omitted":1}}', "malformed metadata", "[]", "null"]
)
def test_transcript_surfaces_metadata_truncation_without_rejecting_intact_body(
    direction: str,
    truncation: str,
) -> None:
    attributes = {
        "input.value": "complete prompt",
        "output.value": "complete answer",
        f"chalk.trace.{direction}.truncation": truncation,
    }
    client, _ = _client(
        {"list_span": [_response({"spans": [_span_dict("span-1", **attributes)]})]}
    )

    text = Trace("session-1", _client=client).transcript(
        wait=False, require_content=True
    )

    assert "complete prompt" in text
    assert "complete answer" in text
    assert "truncated during recording" in text


@pytest.mark.parametrize("direction", ["input", "output"])
def test_transcript_rejects_known_raw_truncation_even_with_inline_value(
    direction: str,
) -> None:
    attributes = {
        "input.value": "prompt",
        "output.value": "answer",
        f"chalk.trace.{direction}.truncation": '{"raw_content":{"reason":"byte_budget_exhausted"}}',
    }
    client, _ = _client(
        {"list_span": [_response({"spans": [_span_dict("span-1", **attributes)]})]}
    )

    with pytest.raises(TraceError, match=f"incomplete: {direction}"):
        Trace("session-1", _client=client).transcript(wait=False, require_content=True)


@pytest.mark.parametrize("value", ["", "x" * 65536, "\u00e9" * 32768])
def test_transcript_preserves_inline_content_at_the_router_byte_boundary(
    value: str,
) -> None:
    """Inline content remains authoritative, including an empty hydrated body."""
    attributes = {
        "input.value": value,
        "output.value": value,
        "chalk.trace.input.content_uri": "s3://private-test-bucket/prompt",
        "chalk.trace.output.content_uri": "s3://private-test-bucket/completion",
    }
    client, _ = _client(
        {"list_span": [_response({"spans": [_span_dict("span-1", **attributes)]})]}
    )

    text = Trace("session-1", _client=client).transcript(
        wait=False, require_content=True, max_chars_per_turn=65536
    )

    assert (
        text == f"### turn 1 (unknown model)\n[prompt]\n{value}\n[completion]\n{value}"
    )


def _empty() -> Any:
    return _response({"spans": []})


def _one_turn() -> Any:
    return _response(
        {
            "spans": [
                _turn(
                    "span-1",
                    "2026-08-17T00:00:01Z",
                    **{"input.value": "hi", "output.value": "yo"},
                )
            ]
        }
    )


def _root_only() -> Any:
    """The session root span every evaluation row has before any LLM call."""
    return _response(
        {"spans": [_turn("root", "2026-08-17T00:00:00Z", **{"chalk.session": "root"})]}
    )


def _root_and_turn() -> Any:
    return _response(
        {
            "spans": [
                _turn("root", "2026-08-17T00:00:00Z", **{"chalk.session": "root"}),
                _turn(
                    "span-1",
                    "2026-08-17T00:00:01Z",
                    **{"input.value": "hi", "output.value": "yo"},
                ),
            ]
        }
    )


def test_transcript_waits_for_turns_still_being_ingested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two empty polls are not a settled session; the turn lands on the third."""
    client, _ = _client({"list_span": [_empty(), _empty(), _one_turn(), _one_turn()]})
    monkeypatch.setattr("chalkcompute._trace.time.sleep", lambda _: None)

    text = Trace("session-1", _client=client).transcript(poll_interval=0)

    assert text.startswith("### turn 1")


def test_transcript_does_not_let_other_spans_end_the_wait_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A steady root span is not a settled transcript; only turns count."""
    client, _ = _client(
        {"list_span": [_root_only(), _root_only(), _root_and_turn(), _root_and_turn()]}
    )
    monkeypatch.setattr("chalkcompute._trace.time.sleep", lambda _: None)

    text = Trace("session-1", _client=client).transcript(poll_interval=0)

    assert text.startswith("### turn 1")


def test_transcript_of_a_session_with_only_other_spans_says_so_after_the_wait() -> None:
    client, _ = _client({"list_span": [_root_only(), _root_only()]})

    text = Trace("session-1", _client=client).transcript(timeout=0, poll_interval=0)

    assert text == "(no LLM turns were recorded in this session)"


def test_transcript_of_a_session_that_never_records_a_span_says_so_after_the_wait() -> (
    None
):
    client, connect = _client({"list_span": [_empty(), _empty()]})

    text = Trace("session-1", _client=client).transcript(timeout=0, poll_interval=0)

    assert text == "(no LLM turns were recorded in this session)"
    # The wait timed out, then one unwaited read confirmed the session is empty.
    assert len(connect.calls) == 2


def test_transcript_does_not_read_a_session_short_while_spans_still_arrive() -> None:
    client, _ = _client({"list_span": [_one_turn(), _one_turn()]})

    with pytest.raises(TimeoutError, match="still receiving"):
        Trace("session-1", _client=client).transcript(timeout=0, poll_interval=0)


def test_transcript_accepts_an_empty_session_when_asked_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client({"list_span": [_empty(), _empty()]})
    monkeypatch.setattr("chalkcompute._trace.time.sleep", lambda _: None)

    text = Trace("session-1", _client=client).transcript(poll_interval=0, expect_min=0)

    assert text == "(no LLM turns were recorded in this session)"


# --- Span decoding --------------------------------------------------------


def test_get_spans_queries_by_session_and_decodes_spans() -> None:
    client, connect = _client(
        {"list_span": [_response({"spans": [_span_dict("span-1")]})]}
    )
    spans = Trace("session-1", _client=client).get_spans(wait=False)

    method, request = connect.calls[0]
    assert method == "list_span"
    assert isinstance(request, trace_pb.ListSpanRequest)
    assert request.query == 'session:"session-1"'

    (span,) = spans
    assert span.span_id == "span-1"
    assert span.kind is SpanKind.CLIENT
    assert span.status_code is SpanStatusCode.OK
    assert span.duration == timedelta(seconds=1.5)
    assert span.session_id == "session-1"


@pytest.mark.parametrize(
    ("session_id", "expected_query"),
    [
        ("run-id:row-id", 'session:"run-id:row-id"'),
        ("rescore-id:run-id:row-id", 'session:"rescore-id:run-id:row-id"'),
        ("agent session", 'session:"agent session"'),
        ('agent "quoted"\\session', r'session:"agent \"quoted\"\\session"'),
    ],
)
def test_get_spans_quotes_session_ids_in_search_filters(
    session_id: str, expected_query: str
) -> None:
    client, connect = _client({"list_span": [_response({"spans": []})]})

    Trace(session_id, _client=client).get_spans(wait=False)

    method, request = connect.calls[0]
    assert method == "list_span"
    assert request.query == expected_query


def test_attribute_float_parses_string_attributes() -> None:
    """`ChalkSpan.attributes` is map<string,string>; summing raw values is a TypeError."""
    client, _ = _client(
        {
            "list_span": [
                _response(
                    {
                        "spans": [
                            _span_dict("span-1", **{"llm.cost": "0.25"}),
                            _span_dict("span-2", **{"llm.cost": "0.75"}),
                        ]
                    },
                )
            ]
        }
    )
    spans = Trace("session-1", _client=client).get_spans(wait=False)
    assert sum(s.attribute_float("llm.cost") or 0.0 for s in spans) == 1.0
    with pytest.raises(TypeError):
        sum(s.attributes["llm.cost"] for s in spans)  # type: ignore[misc]


def test_attribute_accessors_tolerate_missing_and_unparseable_values() -> None:
    client, _ = _client(
        {"list_span": [_response({"spans": [_span_dict("s", note="abc")]})]}
    )
    (span,) = Trace("session-1", _client=client).get_spans(wait=False)
    assert span.attribute_float("absent") is None
    assert span.attribute_float("note") is None
    assert span.attribute_int("absent") is None


def test_list_spans_pages_transparently() -> None:
    client, connect = _client(
        {
            "list_span": [
                _response(
                    {"spans": [_span_dict("span-1")], "nextPageToken": "cursor"},
                ),
                _response({"spans": [_span_dict("span-2")]}),
            ]
        }
    )
    spans = Trace("session-1", _client=client).get_spans(wait=False)
    assert [s.span_id for s in spans] == ["span-1", "span-2"]
    request = connect.calls[1][1]
    assert isinstance(request, trace_pb.ListSpanRequest)
    assert request.page_token == "cursor"


def test_list_spans_honors_limit_without_extra_pages() -> None:
    client, _ = _client(
        {
            "list_span": [
                _response(
                    {
                        "spans": [_span_dict("span-1"), _span_dict("span-2")],
                        "nextPageToken": "cursor",
                    },
                )
            ]
        }
    )
    spans = Trace("session-1", _client=client).get_spans(wait=False, limit=1)
    assert [s.span_id for s in spans] == ["span-1"]


def test_attribute_filters_send_bare_keys_for_none_values() -> None:
    client, connect = _client({"list_span": [_response({"spans": []})]})
    Trace("session-1", _client=client).get_spans(
        wait=False, attribute_filters={"llm.model": "opus", "llm.cost": None}
    )
    request = connect.calls[0][1]
    assert isinstance(request, trace_pb.ListSpanRequest)
    assert [(item.key, item.value or None) for item in request.attribute_filters] == [
        ("llm.model", "opus"),
        ("llm.cost", None),
    ]


# --- Ingest-lag waiting ---------------------------------------------------


def test_get_spans_waits_until_the_span_count_stops_growing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spans arrive asynchronously, so a settled count is the completion signal."""
    client, _ = _client(
        {
            "list_span": [
                _response({"spans": []}),
                _response({"spans": [_span_dict("span-1")]}),
                _response({"spans": [_span_dict("span-1"), _span_dict("span-2")]}),
                _response({"spans": [_span_dict("span-1"), _span_dict("span-2")]}),
            ]
        }
    )
    monkeypatch.setattr("chalkcompute._trace.time.sleep", lambda _: None)
    spans = Trace("session-1", _client=client).get_spans(poll_interval=0)
    assert [s.span_id for s in spans] == ["span-1", "span-2"]


def test_get_spans_raises_rather_than_returning_an_empty_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client({"list_span": [_response({"spans": []})]})
    monkeypatch.setattr("chalkcompute._trace.time.sleep", lambda _: None)
    with pytest.raises(TimeoutError, match="expected at least 1"):
        Trace("session-1", _client=client).get_spans(timeout=0, poll_interval=0)


def test_get_spans_accepts_an_empty_session_when_asked_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(
        {
            "list_span": [
                _response({"spans": []}),
                _response({"spans": []}),
            ]
        }
    )
    monkeypatch.setattr("chalkcompute._trace.time.sleep", lambda _: None)
    assert (
        Trace("session-1", _client=client).get_spans(expect_min=0, poll_interval=0)
        == []
    )


def test_get_spans_raises_when_expect_min_is_never_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(
        {
            "list_span": [
                _response({"spans": [_span_dict("span-1")]}),
                _response({"spans": [_span_dict("span-1")]}),
            ]
        }
    )
    monkeypatch.setattr("chalkcompute._trace.time.sleep", lambda _: None)
    with pytest.raises(TimeoutError, match="expected at least 5"):
        Trace("session-1", _client=client).get_spans(
            expect_min=5, timeout=0, poll_interval=0
        )


def test_get_spans_without_waiting_makes_exactly_one_pass() -> None:
    client, connect = _client({"list_span": [_response({"spans": []})]})
    assert Trace("session-1", _client=client).get_spans(wait=False) == []
    assert len(connect.calls) == 1


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"timeout": -1}, "timeout must be >= 0"),
        ({"poll_interval": -1}, "poll_interval must be >= 0"),
        ({"expect_min": -1}, "expect_min must be >= 0"),
        ({"quiet_for": -1}, "quiet_for must be >= 0"),
        ({"quiet_for": 30, "timeout": 10}, "exceeds timeout"),
    ],
)
def test_get_spans_validates_its_bounds(kwargs: dict[str, Any], match: str) -> None:
    client, _ = _client({})
    with pytest.raises(ValueError, match=match):
        Trace("session-1", _client=client).get_spans(**kwargs)


# --- The quiet window ("trace is complete") ------------------------------


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """A clock that advances only when the code under test sleeps."""
    now = [0.0]
    monkeypatch.setattr("chalkcompute._trace.time.monotonic", lambda: now[0])

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr("chalkcompute._trace.time.sleep", sleep)
    return now


def _one_span_forever() -> list[Any]:
    return [_response({"spans": [_span_dict("span-1")]}) for _ in range(40)]


def test_get_spans_waits_out_the_quiet_window(clock: list[float]) -> None:
    """A steady count returns only once it has held for quiet_for seconds."""
    client, connect = _client({"list_span": _one_span_forever()})

    spans = Trace("session-1", _client=client).get_spans(quiet_for=30, poll_interval=2)

    assert [s.span_id for s in spans] == ["span-1"]
    # Read at 0, 2, ..., 30: the window closes on the sixteenth read.
    assert len(connect.calls) == 16
    assert clock[0] == 30


def test_the_quiet_window_restarts_when_a_span_lands(clock: list[float]) -> None:
    one = _response({"spans": [_span_dict("span-1")]})
    two = _response({"spans": [_span_dict("span-1"), _span_dict("span-2")]})
    client, connect = _client({"list_span": [one, one, two, two, two, two]})

    spans = Trace("session-1", _client=client).get_spans(quiet_for=4, poll_interval=2)

    # 1 at 0s and 2s, then 2 at 4s restarts the window, which closes at 8s.
    assert [s.span_id for s in spans] == ["span-1", "span-2"]
    assert len(connect.calls) == 5
    assert clock[0] == 8


def test_the_quiet_window_does_not_wait_past_the_timeout(clock: list[float]) -> None:
    """A span landing late restarts a window the timeout then cuts short."""
    one = _response({"spans": [_span_dict("span-1")]})
    two = _response({"spans": [_span_dict("span-1"), _span_dict("span-2")]})
    client, _ = _client({"list_span": [one, one, two, two, two, two]})

    # 1 at 0s and 3s; 2 at 6s restarts the window, which cannot close by 10s.
    with pytest.raises(TimeoutError, match="held at 2 span"):
        Trace("session-1", _client=client).get_spans(
            quiet_for=6, timeout=10, poll_interval=3
        )


def test_the_environment_sets_the_quiet_window(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pod carries the environment's setting; a scorer passes nothing."""
    monkeypatch.setenv("CHALK_TRACE_COMPLETE_AFTER_SECONDS", "30")
    client, connect = _client({"list_span": _one_span_forever()})

    Trace("session-1", _client=client).get_spans(poll_interval=2)

    assert len(connect.calls) == 16


def test_an_explicit_quiet_window_overrides_the_environment(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHALK_TRACE_COMPLETE_AFTER_SECONDS", "30")
    client, connect = _client({"list_span": _one_span_forever()})

    Trace("session-1", _client=client).get_spans(quiet_for=0, poll_interval=2)

    assert len(connect.calls) == 2


def test_a_blank_environment_setting_means_no_quiet_window(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHALK_TRACE_COMPLETE_AFTER_SECONDS", " ")
    client, connect = _client({"list_span": _one_span_forever()})

    Trace("session-1", _client=client).get_spans(poll_interval=2)

    assert len(connect.calls) == 2


@pytest.mark.parametrize("raw", ["soon", "-5"])
def test_a_malformed_environment_setting_is_an_error(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHALK_TRACE_COMPLETE_AFTER_SECONDS", raw)
    client, _ = _client({})

    with pytest.raises(ValueError, match="CHALK_TRACE_COMPLETE_AFTER_SECONDS"):
        Trace("session-1", _client=client).get_spans()


def test_the_quiet_window_counts_every_span_in_the_session(
    clock: list[float],
) -> None:
    """Tool spans between LLM turns keep the window open: the session is not quiet."""
    turn_1 = _turn(
        "span-1", "2026-08-17T00:00:00Z", **{"input.value": "hi", "output.value": "yo"}
    )
    tools = [
        _turn(f"tool-{n}", f"2026-08-17T00:00:{10 * n:02d}Z", **{"tool.name": "bash"})
        for n in (1, 2, 3)
    ]
    turn_2 = _turn(
        "span-2", "2026-08-17T00:00:40Z", **{"input.value": "go", "output.value": "ok"}
    )
    # A tool span lands at 10s, 20s and 30s, then the next turn at 40s.
    seen = [
        [turn_1],
        [turn_1, tools[0]],
        [turn_1, *tools[:2]],
        [turn_1, *tools],
        [turn_1, *tools, turn_2],
    ]
    responses = [_response({"spans": spans}) for spans in seen]
    responses += [_response({"spans": seen[-1]}) for _ in range(4)]
    client, _ = _client({"list_span": responses})

    text = Trace("session-1", _client=client).transcript(
        quiet_for=30, timeout=120, poll_interval=10
    )

    # Quiet only from 40s, so the window closes at 70s, with both turns.
    assert "### turn 2" in text
    assert clock[0] == 70


def test_an_unwaited_read_ignores_the_environments_quiet_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wait=False never uses the window, so the setting cannot invalidate it."""
    monkeypatch.setenv("CHALK_TRACE_COMPLETE_AFTER_SECONDS", "30")
    client, connect = _client({"list_span": [_one_turn(), _one_turn()]})
    trace = Trace("session-1", _client=client)

    assert len(trace.get_spans(wait=False, timeout=0)) == 1
    assert trace.transcript(wait=False, timeout=0).startswith("### turn 1")
    assert len(connect.calls) == 2


def test_transcript_honors_the_environments_quiet_window(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The judge path: llm_judge calls transcript() with no arguments."""
    monkeypatch.setenv("CHALK_TRACE_COMPLETE_AFTER_SECONDS", "30")
    client, connect = _client({"list_span": [_one_turn() for _ in range(40)]})

    text = Trace("session-1", _client=client).transcript(poll_interval=2)

    assert text.startswith("### turn 1")
    assert len(connect.calls) == 16


# --- Session and trace summaries -----------------------------------------


def test_summary_reads_the_session_aggregate() -> None:
    client, connect = _client(
        {
            "search_sessions": [
                _response(
                    {
                        "sessions": [
                            {
                                "sessionId": "session-1",
                                "startTime": "2026-08-17T00:00:00Z",
                                "endTime": "2026-08-17T00:05:00Z",
                                "duration": "300s",
                                "traceCount": "4",
                                "spanCount": 27,
                                "firstRootSpanName": "agent.turn",
                            }
                        ]
                    },
                )
            ]
        }
    )
    summary = Trace("session-1", _client=client).summary(wait=False)
    assert connect.calls[0][0] == "search_sessions"
    request = connect.calls[0][1]
    assert isinstance(request, trace_pb.SearchSessionsRequest)
    assert request.session_ids == ["session-1"]
    assert summary.trace_count == 4
    assert summary.span_count == 27
    assert summary.duration == timedelta(minutes=5)
    assert summary.first_root_span_name == "agent.turn"


def test_summary_raises_not_found_for_an_unknown_session() -> None:
    client, _ = _client({"search_sessions": [_response({"sessions": []})]})
    with pytest.raises(TraceNotFoundError, match="Session not found"):
        Trace("session-1", _client=client).summary(wait=False)


def test_traces_lists_one_summary_per_turn() -> None:
    client, connect = _client(
        {
            "search_trace_summaries": [
                _response(
                    {
                        "traceSummaries": [
                            {
                                "traceId": "trace-1",
                                "spanCount": 3,
                                "rootSpanNames": ["agent.turn"],
                                "sessionId": "session-1",
                            }
                        ]
                    },
                )
            ]
        }
    )
    (summary,) = Trace("session-1", _client=client).traces(wait=False)
    request = connect.calls[0][1]
    assert isinstance(request, trace_pb.SearchTraceSummariesRequest)
    assert request.session_ids == ["session-1"]
    assert summary.trace_id == "trace-1"
    assert summary.root_span_names == ("agent.turn",)
    assert summary.session_id == "session-1"


# --- Errors ---------------------------------------------------------------


def test_permission_denied_names_the_missing_permission() -> None:
    client, _ = _client(
        {"list_span": [ConnectRpcError(Code.PERMISSION_DENIED, "denied")]}
    )
    with pytest.raises(TraceError, match="monitoring-read permission"):
        Trace("session-1", _client=client).get_spans(wait=False)


def test_transport_failure_surfaces_the_status_code() -> None:
    client, _ = _client({"list_span": [ConnectRpcError(Code.INTERNAL, "boom")]})
    with pytest.raises(TraceError, match="boom") as exc:
        Trace("session-1", _client=client).get_spans(wait=False)
    assert exc.value.status_code == 500


def test_client_rejects_two_transports() -> None:
    with pytest.raises(ValueError, match="only one of"):
        TraceClient(chalk_client=object(), connect_client=_FakeConnectClient({}))


# --- Every read settles on the same signal -------------------------------


def _session_response(
    span_count: int, trace_count: int = 1
) -> trace_pb.SearchSessionsResponse:
    return _response(
        {
            "sessions": [
                {
                    "sessionId": "session-1",
                    "traceCount": trace_count,
                    "spanCount": span_count,
                }
            ]
        },
    )


def _traces_response(*span_counts: int) -> trace_pb.SearchTraceSummariesResponse:
    return _response(
        {
            "traceSummaries": [
                {"traceId": f"trace-{index}", "spanCount": count}
                for index, count in enumerate(span_counts)
            ]
        },
    )


def test_summary_waits_until_the_session_span_count_stops_growing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(
        {
            "search_sessions": [
                _response({"sessions": []}),
                _session_response(3, trace_count=1),
                _session_response(11, trace_count=2),
                _session_response(11, trace_count=2),
            ]
        }
    )
    monkeypatch.setattr("chalkcompute._trace.time.sleep", lambda _: None)
    summary = Trace("session-1", _client=client).summary(poll_interval=0)
    assert (summary.trace_count, summary.span_count) == (2, 11)


def test_summary_treats_an_absent_session_as_not_yet_ingested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session with no spans yet is missing, not an error to give up on."""
    client, connect = _client(
        {
            "search_sessions": [
                _response({"sessions": []}),
                _response({"sessions": []}),
                _session_response(2),
                _session_response(2),
            ]
        }
    )
    monkeypatch.setattr("chalkcompute._trace.time.sleep", lambda _: None)
    assert Trace("session-1", _client=client).summary(poll_interval=0).span_count == 2
    assert len(connect.calls) == 4


def test_summary_raises_not_found_when_the_session_never_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The specific diagnosis survives waiting: it received no spans at all."""
    client, _ = _client({"search_sessions": [_response({"sessions": []})]})
    monkeypatch.setattr("chalkcompute._trace.time.sleep", lambda _: None)
    with pytest.raises(TraceNotFoundError, match="Session not found"):
        Trace("session-1", _client=client).summary(timeout=0, poll_interval=0)


def test_summary_accepts_an_empty_session_when_asked_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(
        {
            "search_sessions": [
                _session_response(0, trace_count=0),
                _session_response(0, trace_count=0),
            ]
        }
    )
    monkeypatch.setattr("chalkcompute._trace.time.sleep", lambda _: None)
    summary = Trace("session-1", _client=client).summary(expect_min=0, poll_interval=0)
    assert summary.span_count == 0


def test_traces_waits_for_a_turn_that_is_still_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A growing turn adds spans without adding a trace, so the count is the signal."""
    client, _ = _client(
        {
            "search_trace_summaries": [
                _traces_response(2),
                _traces_response(5),
                _traces_response(5),
            ]
        }
    )
    monkeypatch.setattr("chalkcompute._trace.time.sleep", lambda _: None)
    (summary,) = Trace("session-1", _client=client).traces(poll_interval=0)
    assert summary.span_count == 5


def test_traces_waits_for_a_later_turn_to_appear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(
        {
            "search_trace_summaries": [
                _traces_response(3),
                _traces_response(3, 4),
                _traces_response(3, 4),
            ]
        }
    )
    monkeypatch.setattr("chalkcompute._trace.time.sleep", lambda _: None)
    summaries = Trace("session-1", _client=client).traces(poll_interval=0)
    assert [s.span_count for s in summaries] == [3, 4]


def test_traces_raises_rather_than_returning_an_empty_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client({"search_trace_summaries": [_traces_response()]})
    monkeypatch.setattr("chalkcompute._trace.time.sleep", lambda _: None)
    with pytest.raises(TimeoutError, match="expected at least 1"):
        Trace("session-1", _client=client).traces(timeout=0, poll_interval=0)


@pytest.mark.parametrize("method", ["get_spans", "summary", "traces"])
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"timeout": -1}, "timeout must be >= 0"),
        ({"poll_interval": -1}, "poll_interval must be >= 0"),
        ({"expect_min": -1}, "expect_min must be >= 0"),
    ],
)
def test_every_read_validates_the_same_bounds(
    method: str, kwargs: dict[str, Any], match: str
) -> None:
    client, _ = _client({})
    with pytest.raises(ValueError, match=match):
        getattr(Trace("session-1", _client=client), method)(**kwargs)


@pytest.mark.parametrize(
    ("method", "responses"),
    [
        ("get_spans", {"list_span": [_response({"spans": []})]}),
        ("summary", {"search_sessions": [_session_response(1)]}),
        ("traces", {"search_trace_summaries": [_traces_response(1)]}),
    ],
)
def test_every_read_makes_exactly_one_pass_without_waiting(
    method: str, responses: dict[str, list[Any]]
) -> None:
    client, connect = _client(responses)
    getattr(Trace("session-1", _client=client), method)(wait=False)
    assert len(connect.calls) == 1
