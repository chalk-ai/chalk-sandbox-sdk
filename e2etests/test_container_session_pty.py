"""End-to-end tests for pty-backed host container sessions."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from chalkcompute import Container, ContainerSession
from e2etests.container_session_helpers import (
    collect_session,
    detach_quietly,
    new_host_container,
    next_event,
)
from e2etests.conftest import needs_staging


@pytest.fixture
def host_container() -> Iterator[Container]:
    container = new_host_container("test-session-pty")
    try:
        yield container
    finally:
        container.stop()


def _pty_until(session: ContainerSession, expected: str) -> str:
    text = ""
    for event in session.events():
        match event:
            case ContainerSession.ErrorEvent() as error:
                pytest.fail(f"Session {session.id} errored: {error.error}")
            case (
                ContainerSession.ExitedEvent()
                | ContainerSession.FailedEvent()
                | ContainerSession.TimedOutEvent()
            ):
                pytest.fail(
                    f"Session {session.id} exited before pty output contained {expected!r}: {text!r}"
                )
            case ContainerSession.OutputEvent(
                stream=ContainerSession.OutputEvent.Stream.PTY_OUTPUT,
            ) as output:
                text += output.text
                if expected in text:
                    return text
    pytest.fail(f"Session {session.id} ended before pty output contained {expected!r}")


def _assert_echoed_output(text: str, value: str, output: str) -> None:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    assert f"{value}\n{output}" in normalized


@needs_staging
def test_pty_size(host_container: Container) -> None:
    session = host_container.new_session(
        "sh",
        "-c",
        "stty size; read _; stty size",
        pty=(81, 25),
        timeout_secs=10,
    )

    assert host_container.get_session(session.id).pty
    assert "25 81" in _pty_until(session, "25 81")

    session.resize(100, 40)
    session.write("\n")

    collected = collect_session(session)
    assert collected.ok
    assert "40 100" in collected.pty_text


@needs_staging
def test_pty_parallel(host_container: Container) -> None:
    first = host_container.new_session(
        "sh",
        "-c",
        "stty echo; printf 'first-ready\\n'; "
        "read value; printf 'first:%s\\n' \"$value\"",
        pty=True,
        timeout_secs=10,
    )
    second = host_container.new_session(
        "sh",
        "-c",
        "stty echo; printf 'second-ready\\n'; "
        "read value; printf 'second:%s\\n' \"$value\"",
        pty=True,
        timeout_secs=10,
    )
    sessions = [first, second]
    try:
        assert "first-ready" in _pty_until(first, "first-ready")
        assert "second-ready" in _pty_until(second, "second-ready")

        infos = {item.id: item for item in host_container.list_sessions()}
        assert {first.id, second.id} <= set(infos)
        assert infos[first.id].pty
        assert infos[second.id].pty
        assert infos[first.id].status.state is ContainerSession.Status.State.RUNNING
        assert infos[second.id].status.state is ContainerSession.Status.State.RUNNING

        first.write("alpha\n")
        second.write("beta\n")

        first_collected = collect_session(first)
        second_collected = collect_session(second)

        assert first_collected.ok
        assert second_collected.ok
        _assert_echoed_output(first_collected.pty_text, "alpha", "first:alpha")
        _assert_echoed_output(second_collected.pty_text, "beta", "second:beta")
    finally:
        for session in sessions:
            detach_quietly(session)


@needs_staging
def test_pty_multi_client(host_container: Container) -> None:
    starter = host_container.new_session(
        "sh",
        "-c",
        "stty echo; printf 'ready\\n'; "
        "while IFS= read -r line; do "
        "printf 'line:%s\\n' \"$line\"; "
        "[ \"$line\" = done ] && exit 0; "
        "done",
        pty=True,
        timeout_secs=10,
    )

    assert "ready" in _pty_until(starter, "ready")
    starter.detach()

    clients: list[ContainerSession] = []
    try:
        source = host_container.attach_session(starter.id, pty=True)
        left = host_container.attach_session(starter.id, pty=True)
        right = host_container.attach_session(starter.id, pty=True)
        clients.extend([source, left, right])

        # Both observers should see source's pty input echo and process output.
        source.write("from-source\n")
        _assert_echoed_output(
            _pty_until(left, "line:from-source"),
            "from-source",
            "line:from-source",
        )
        _assert_echoed_output(
            _pty_until(right, "line:from-source"),
            "from-source",
            "line:from-source",
        )

        # A different attached client should be able to drive the same process.
        left.write("from-left\n")
        _assert_echoed_output(
            _pty_until(source, "line:from-left"),
            "from-left",
            "line:from-left",
        )
        _assert_echoed_output(
            _pty_until(right, "line:from-left"),
            "from-left",
            "line:from-left",
        )

        right.write("done\n")
        collected = collect_session(source)
        assert collected.ok
        assert "line:done" in collected.pty_text
    finally:
        for client in clients:
            detach_quietly(client)


@needs_staging
def test_pty_reattach(host_container: Container) -> None:
    session = host_container.new_session(
        "sh",
        "-c",
        "stty echo; printf 'ready\\n'; "
        "read first; printf 'first:%s\\n' \"$first\"; "
        "read second; printf 'second:%s\\n' \"$second\"",
        pty=True,
        timeout_secs=10,
    )

    assert "ready" in _pty_until(session, "ready")

    session.write("one\n")
    _assert_echoed_output(_pty_until(session, "first:one"), "one", "first:one")

    session.detach()
    assert (
        host_container.get_session(session.id).status.state
        is ContainerSession.Status.State.RUNNING
    )

    reattached = host_container.attach_session(session.id, pty=True)
    try:
        reattached.write("two\n")

        collected = collect_session(reattached)
        assert collected.ok
        _assert_echoed_output(collected.pty_text, "two", "second:two")
    finally:
        detach_quietly(reattached)


@needs_staging
def test_pty_signal(host_container: Container) -> None:
    session = host_container.new_session(
        "sh",
        "-c",
        "trap 'printf got-term; exit 7' TERM; printf 'ready\\n'; read value",
        pty=True,
        timeout_secs=10,
    )

    assert "ready" in _pty_until(session, "ready")

    session.signal(15)
    collected = collect_session(session)

    assert collected.exit_code == 7
    assert "got-term" in collected.pty_text


@needs_staging
def test_pty_invalid_signal(host_container: Container) -> None:
    session = host_container.new_session(
        "sh",
        "-c",
        "printf 'ready\\n'; sleep 30",
        pty=True,
    )
    try:
        assert "ready" in _pty_until(session, "ready")

        session.signal(0)
        event = next_event(
            session,
            lambda item: isinstance(item, ContainerSession.ErrorEvent),
        )

        assert isinstance(event, ContainerSession.ErrorEvent)
        assert event.code == "invalid_request"
        assert session.status().state is ContainerSession.Status.State.RUNNING
    finally:
        session.detach()
