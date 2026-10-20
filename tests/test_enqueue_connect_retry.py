from __future__ import annotations

import pytest

from chalkcompute import _remote_call_client as transport


CONNECT_REFUSED = (
    'gRPC status: status: Unavailable, message: "upstream connect error or '
    "disconnect/reset before headers. reset reason: remote connection failure, "
    'transport failure reason: delayed connect error: Connection refused"'
)


class QueueClient:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.attempts = []
        self.accepted = []

    def call_ipc(self, name, input_ipc, metadata=None):
        raise AssertionError("enqueue recovery must not invoke the function directly")

    def enqueue(self, *args):
        self.attempts.append(args)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        self.accepted.append(args)
        return outcome


def client_with_outcomes(monkeypatch, outcomes):
    inner = QueueClient(outcomes)
    monkeypatch.setattr(transport, "_NativeRemoteCallClient", lambda *args: inner)
    return transport.RemoteCallClient("queue.chalk.test:443"), inner


@pytest.mark.parametrize("metadata", [(), (("traceparent", "trace-123"),)])
def test_retries_only_the_unaccepted_enqueue(monkeypatch, metadata):
    sleeps = []
    monkeypatch.setattr(transport.time, "sleep", sleeps.append)
    client, inner = client_with_outcomes(
        monkeypatch,
        [
            "call-1",
            transport.RemoteCallUnavailableError(CONNECT_REFUSED),
            transport.RemoteCallUnavailableError(CONNECT_REFUSED),
            "call-2",
        ],
    )

    # An orchestrator may already have enqueued work before another submission
    # fails. Retrying that submission must not replay its earlier work.
    call_ids = [client.enqueue("embed", value, metadata) for value in (b"one", b"two")]

    assert call_ids == ["call-1", "call-2"]
    suffix = (list(metadata),) if metadata else ()
    assert inner.attempts == [
        ("embed", b"one", *suffix),
        ("embed", b"two", *suffix),
        ("embed", b"two", *suffix),
        ("embed", b"two", *suffix),
    ]
    assert len(inner.accepted) == 2
    assert sleeps == [0.1, 0.2]


@pytest.mark.parametrize(
    "error",
    [
        transport.RemoteCallUnavailableError("unavailable"),
        transport.RemoteCallUnavailableError("upstream request timeout"),
        transport.RemoteCallUnavailableError(
            "upstream connect error or disconnect/reset before headers. "
            "reset reason: connection termination"
        ),
        transport.RemoteCallUnavailableError("GOAWAY"),
        transport.RemoteCallUnavailableError("connection refused"),
        transport.RemoteCallTransportError(CONNECT_REFUSED),
        RuntimeError(CONNECT_REFUSED),
    ],
    ids=[
        "unavailable",
        "timeout",
        "reset",
        "goaway",
        "unqualified",
        "transport",
        "handler",
    ],
)
def test_does_not_retry_ambiguous_or_execution_errors(monkeypatch, error):
    client, inner = client_with_outcomes(monkeypatch, [error, "duplicate"])

    with pytest.raises(type(error)) as caught:
        client.enqueue("embed", b"one")

    assert caught.value is error
    assert len(inner.attempts) == 1
    assert inner.accepted == []


def test_enqueue_retry_has_a_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(transport, "_DIRECT_CALL_RETRY_TIMEOUT_SECONDS", 0.25)

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(transport.time, "sleep", sleep)
    errors = [transport.RemoteCallUnavailableError(CONNECT_REFUSED) for _ in range(3)]
    client, inner = client_with_outcomes(monkeypatch, [*errors, "too-late"])

    with pytest.raises(transport.RemoteCallUnavailableError) as caught:
        client.enqueue("embed", b"one")

    assert caught.value is errors[-1]
    assert now[0] == 0.25
    assert len(inner.attempts) == 3
    assert inner.accepted == []
