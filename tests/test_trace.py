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
from chalkcompute._connect import use_connect_client
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
    with use_connect_client(connect):  # type: ignore[arg-type]
        return TraceClient(), connect


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


# --- Span decoding --------------------------------------------------------


def test_get_spans_queries_by_session_and_decodes_spans() -> None:
    client, connect = _client(
        {"list_span": [_response({"spans": [_span_dict("span-1")]})]}
    )
    spans = Trace("session-1", _client=client).get_spans(wait=False)

    method, request = connect.calls[0]
    assert method == "list_span"
    assert isinstance(request, trace_pb.ListSpanRequest)
    assert request.query == "session:session-1"

    (span,) = spans
    assert span.span_id == "span-1"
    assert span.kind is SpanKind.CLIENT
    assert span.status_code is SpanStatusCode.OK
    assert span.duration == timedelta(seconds=1.5)
    assert span.session_id == "session-1"


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
    ],
)
def test_get_spans_validates_its_bounds(kwargs: dict[str, Any], match: str) -> None:
    client, _ = _client({})
    with pytest.raises(ValueError, match=match):
        Trace("session-1", _client=client).get_spans(**kwargs)


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


def test_explicit_chalk_client_wins_over_scoped_override() -> None:
    override = _FakeConnectClient({})
    chalk_client = object()

    with use_connect_client(override):  # type: ignore[arg-type]
        client = TraceClient(chalk_client=chalk_client)

    assert client._connect is not override
    assert client._connect._chalk_client is chalk_client
    client._connect.close()


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
