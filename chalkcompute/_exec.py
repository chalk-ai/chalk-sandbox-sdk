"""Exec errors and a bounded wait for the final RPC status."""

from collections.abc import Iterator
from contextvars import ContextVar
from threading import Event, Timer
from typing import Generic, TypeVar

from connectrpc.code import Code
from connectrpc.errors import ConnectError
from pyqwest import SyncClient, SyncRequest, SyncResponse

from chalkcompute._http import _SYNC_HTTP_TRANSPORT

_Result = TypeVar("_Result")
_Response = TypeVar("_Response")
FINAL_STATUS_TIMEOUT_SECONDS = 30.0


class ExecStreamError(ConnectError, Generic[_Result]):
    """An exec RPC error with its known command result."""

    def __init__(self, error: ConnectError, result: _Result) -> None:
        super().__init__(error.code, error.message, error.details)
        self.result = result


# Capture the HTTP response for this iterator, never another concurrent RPC.
_active_stream: ContextVar["FinalStatusStream | None"] = ContextVar(
    "exec_final_status_stream", default=None
)


class _ExecTransport:
    def execute_sync(self, request: SyncRequest) -> SyncResponse:
        response = _SYNC_HTTP_TRANSPORT.execute_sync(request)
        if (stream := _active_stream.get()) is not None:
            stream.response = response
        return response


EXEC_HTTP_CLIENT = SyncClient(transport=_ExecTransport())


class FinalStatusStream(Iterator[_Response]):
    def __init__(self, responses: Iterator[_Response]) -> None:
        self._responses = responses
        self.response: SyncResponse | None = None
        self._timer: Timer | None = None
        self._expired = Event()

    def __iter__(self) -> Iterator[_Response]:
        return self

    def __next__(self) -> _Response:
        token = _active_stream.set(self)
        try:
            if self._expired.is_set():
                raise TimeoutError
            response = next(self._responses)
            if self._expired.is_set():
                raise TimeoutError
            return response
        except BaseException as exc:
            if self._timer is not None:
                self._timer.cancel()
            if self._expired.is_set() and isinstance(exc, Exception):
                raise ConnectError(
                    Code.DEADLINE_EXCEEDED,
                    "Timed out waiting for the final exec RPC status",
                ) from exc
            raise
        finally:
            _active_stream.reset(token)

    def start_timeout(self) -> None:
        if self._timer is not None or self.response is None:
            return
        response = self.response

        def expire() -> None:
            self._expired.set()
            # Closing the HTTP body interrupts next(); closing its Python generator cannot.
            response.close()

        self._timer = Timer(FINAL_STATUS_TIMEOUT_SECONDS, expire)
        self._timer.daemon = True
        self._timer.start()


def start_final_status_timeout(responses: Iterator[object]) -> None:
    if isinstance(responses, FinalStatusStream):
        responses.start_timeout()
