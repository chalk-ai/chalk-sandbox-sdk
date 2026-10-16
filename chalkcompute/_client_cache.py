"""One shared client, with safe retirement while requests are using it."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generic, TypeVar

logger = logging.getLogger(__name__)
ClientT = TypeVar("ClientT")


def _is_closed(client: object) -> bool:
    return bool(getattr(client, "_closed", False) or getattr(client, "closed", False))


def _close(client: object | None) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.debug("Failed to close retired client", exc_info=True)


@dataclass
class _Entry(Generic[ClientT]):
    client: ClientT
    key: object
    borrowers: int = 0
    retired: bool = False


class ClientCache(Generic[ClientT]):
    """Create one client at a time; close retired clients after their last RPC.

    ``key`` identifies the credentials used to create a client. Getting and
    borrowing share one acquisition path, so a borrower is registered before
    retirement can intervene. Network work and cleanup never hold the lock.
    """

    def __init__(self, factory: Callable[[], ClientT]) -> None:
        self._factory = factory
        self._condition = threading.Condition()
        self._current: _Entry[ClientT] | None = None
        self._connecting = False
        self._generation = 0

    @property
    def client(self) -> ClientT | None:
        """Inspect the cached client without creating or borrowing one."""
        with self._condition:
            return self._current.client if self._current else None

    def get(self, *, key: object = None) -> ClientT:
        return self._acquire(key, borrow=False).client

    @contextmanager
    def borrow(self, *, key: object = None) -> Generator[ClientT, None, None]:
        entry = self._acquire(key, borrow=True)
        try:
            yield entry.client
        finally:
            with self._condition:
                entry.borrowers -= 1
                close = entry.retired and entry.borrowers == 0
            if close:
                _close(entry.client)

    def retire(self, *, expected: ClientT | None = None) -> None:
        """Stop new borrowers; an old client's error cannot evict its replacement."""
        with self._condition:
            if expected is not None and (
                self._current is None or self._current.client is not expected
            ):
                return
            # Also invalidate a connection being built during a reset.
            self._generation += 1
            close = self._detach_current()
        _close(close)

    def _detach_current(self) -> ClientT | None:
        """Called under the lock; return an idle client for cleanup outside it."""
        entry, self._current = self._current, None
        if entry is None:
            return None
        entry.retired = True
        return entry.client if entry.borrowers == 0 else None

    def _acquire(self, key: object, *, borrow: bool) -> _Entry[ClientT]:
        while True:
            with self._condition:
                entry = self._current
                if entry and entry.key == key and not _is_closed(entry.client):
                    if borrow:
                        entry.borrowers += 1
                    return entry
                if self._connecting:
                    self._condition.wait_for(lambda: not self._connecting)
                    continue
                close = self._detach_current()
                self._connecting = True
                generation = self._generation

            try:
                _close(close)
                client = self._factory()
            except BaseException:
                self._finish_connecting()
                raise

            with self._condition:
                self._connecting = False
                self._condition.notify_all()
                if generation == self._generation:
                    entry = _Entry(client, key, borrowers=int(borrow))
                    self._current = entry
                    return entry

            # A reset happened during connection creation. Discard it and retry.
            _close(client)

    def _finish_connecting(self) -> None:
        with self._condition:
            self._connecting = False
            self._condition.notify_all()
