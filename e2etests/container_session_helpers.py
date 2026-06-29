"""Shared helpers for container session e2e tests."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from chalkcompute import (
    ComputeClass,
    Container,
    ContainerError,
    ContainerSession,
)


@dataclass(frozen=True)
class CollectedSession:
    event: ContainerSession.Event
    stdout: bytes = b""
    stderr: bytes = b""
    pty_output: bytes = b""

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")

    @property
    def pty_text(self) -> str:
        return self.pty_output.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return (
            isinstance(self.event, ContainerSession.ExitedEvent)
            and self.event.status.exit_code == 0
        )

    @property
    def exit_code(self) -> int | None:
        if isinstance(
            self.event,
            ContainerSession.ExitedEvent | ContainerSession.TimedOutEvent,
        ):
            return self.event.status.exit_code
        return None


def new_host_container(prefix: str) -> Container:
    container = Container(
        image="debian:bookworm-slim",
        name=f"{prefix}-{uuid.uuid4().hex[:8]}",
        compute_class=ComputeClass.HOST,
        entrypoint=["/bin/sh", "-c", "sleep 600"],
    )
    try:
        return container.run()
    except BaseException:
        if container.id is not None:
            try:
                container.stop()
            except ContainerError:
                pass
        raise


def next_event(
    session: ContainerSession,
    predicate: Callable[[ContainerSession.Event], bool],
) -> ContainerSession.Event:
    for event in session.events():
        if predicate(event):
            return event
    pytest.fail(f"Session {session.id} ended before the expected event arrived")


def collect_session(session: ContainerSession) -> CollectedSession:
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    pty_parts: list[bytes] = []

    for event in session.events():
        match event:
            case ContainerSession.OutputEvent(
                stream=ContainerSession.OutputEvent.Stream.STDOUT,
                data=data,
            ):
                stdout_parts.append(data)
            case ContainerSession.OutputEvent(
                stream=ContainerSession.OutputEvent.Stream.STDERR,
                data=data,
            ):
                stderr_parts.append(data)
            case ContainerSession.OutputEvent(
                stream=ContainerSession.OutputEvent.Stream.PTY_OUTPUT,
                data=data,
            ):
                pty_parts.append(data)
            case (
                ContainerSession.ExitedEvent()
                | ContainerSession.FailedEvent()
                | ContainerSession.TimedOutEvent()
                | ContainerSession.ErrorEvent()
            ):
                return CollectedSession(
                    event=event,
                    stdout=b"".join(stdout_parts),
                    stderr=b"".join(stderr_parts),
                    pty_output=b"".join(pty_parts),
                )
            case ContainerSession.DetachedEvent():
                pytest.fail(f"Session {session.id} detached before it ended")

    pytest.fail(f"Session {session.id} ended before a done event arrived")


def detach_quietly(session: ContainerSession) -> None:
    try:
        session.detach()
    except ContainerError:
        pass
