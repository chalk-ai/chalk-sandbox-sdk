"""End-to-end tests for persistent host container sessions."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from chalkcompute import (
    Container,
    ContainerError,
    ContainerSession,
)
from e2etests.container_session_helpers import (
    collect_session,
    detach_quietly,
    new_host_container,
    next_event,
)
from e2etests.conftest import needs_staging


@pytest.fixture
def host_container() -> Iterator[Container]:
    container = new_host_container("test-session")
    try:
        yield container
    finally:
        container.stop()


@needs_staging
def test_session_smoke(host_container: Container) -> None:
    session = host_container.new_session(
        "sh",
        "-c",
        "printf 'session-smoke\\n'",
    )

    assert session.id
    assert host_container.get_session(session.id).id == session.id

    collected = collect_session(session)
    assert collected.ok
    assert collected.stdout_text == "session-smoke\n"


@needs_staging
def test_session_stdin(host_container: Container) -> None:
    session = host_container.new_session(
        "sh",
        "-c",
        'read value; printf "got:%s\\n" "$value"',
    )

    session.write("input-value\n")
    session.close_stdin()

    collected = collect_session(session)
    assert collected.ok
    assert collected.stdout_text == "got:input-value\n"


@needs_staging
def test_session_parallel(host_container: Container) -> None:
    first = host_container.new_session(
        "sh",
        "-c",
        'read value; printf "first:%s\\n" "$value"',
        timeout_secs=10,
    )
    second = host_container.new_session(
        "sh",
        "-c",
        'read value; printf "second:%s\\n" "$value"',
        timeout_secs=10,
    )
    sessions = [first, second]
    try:
        infos = {item.id: item for item in host_container.list_sessions()}
        assert {first.id, second.id} <= set(infos)
        assert infos[first.id].status.state is ContainerSession.Status.State.RUNNING
        assert infos[second.id].status.state is ContainerSession.Status.State.RUNNING

        first.write("alpha\n")
        second.write("beta\n")
        first.close_stdin()
        second.close_stdin()

        first_collected = collect_session(first)
        second_collected = collect_session(second)

        assert first_collected.ok
        assert second_collected.ok
        assert first_collected.stdout_text == "first:alpha\n"
        assert second_collected.stdout_text == "second:beta\n"
    finally:
        for session in sessions:
            detach_quietly(session)


@needs_staging
def test_session_list_isolation() -> None:
    # Sessions should be visible only through their owning container.
    left = new_host_container("test-session-left")
    right = new_host_container("test-session-right")
    session: ContainerSession | None = None
    try:
        session = left.new_session("sh", "-c", "sleep 30")

        info = left.get_session(session.id)
        assert info.id == session.id
        assert info.command == "sh"
        assert info.args == ["-c", "sleep 30"]
        assert info.status.state is ContainerSession.Status.State.RUNNING

        assert [item.id for item in left.list_sessions()] == [session.id]
        assert right.list_sessions() == []
        with pytest.raises(ContainerError):
            right.get_session(session.id)
    finally:
        if session is not None:
            session.detach()
        left.stop()
        right.stop()


@needs_staging
def test_session_reattach(
    host_container: Container,
) -> None:
    # Reattaching should preserve the running process and its stdin.
    session = host_container.new_session(
        "sh",
        "-c",
        'read first; printf "first:%s\\n" "$first"; '
        'read second; printf "second:%s\\n" "$second"',
    )

    session.write("one\n")
    first_output = next_event(
        session,
        lambda event: isinstance(event, ContainerSession.OutputEvent)
        and event.stream is ContainerSession.OutputEvent.Stream.STDOUT
        and event.text == "first:one\n",
    )
    assert isinstance(first_output, ContainerSession.OutputEvent)
    assert first_output.text == "first:one\n"

    session.detach()

    reattached = host_container.attach_session(session.id)
    reattached.write("two\n")
    reattached.close_stdin()

    collected = collect_session(reattached)
    assert collected.ok
    assert collected.stdout_text == "second:two\n"


@needs_staging
def test_session_multi_attach(
    host_container: Container,
) -> None:
    # Multiple attachments should interact with the same underlying process.
    session = host_container.new_session(
        "sh",
        "-c",
        'read value; printf "shared:%s\\n" "$value"',
    )
    session.detach()

    observer = host_container.attach_session(session.id)
    writer = host_container.attach_session(session.id)
    writer.write("from-writer\n")
    writer.close_stdin()

    collected = collect_session(observer)
    assert collected.ok
    assert collected.stdout_text == "shared:from-writer\n"


@needs_staging
def test_session_timeout(host_container: Container) -> None:
    session = host_container.new_session(
        "sh",
        "-c",
        "sleep 30",
        timeout_secs=1,
    )

    result = session.wait()
    assert result.timed_out
    assert (
        host_container.get_session(session.id).status.state
        is ContainerSession.Status.State.TIMED_OUT
    )


@needs_staging
def test_session_invalid_signal(
    host_container: Container,
) -> None:
    # Signal 0 should report an API error without killing the process.
    session = host_container.new_session("sh", "-c", "sleep 30")
    try:
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


@needs_staging
def test_session_bad_signal(
    host_container: Container,
) -> None:
    session = host_container.new_session("sh", "-c", "sleep 30")

    session.signal(999)
    result = session.wait()

    assert result.failed
    assert result.failure_message
    assert (
        host_container.get_session(session.id).status.state
        is ContainerSession.Status.State.FAILED
    )
