"""Exec errors and a bounded wait for the final RPC status."""

from collections.abc import Iterator, Mapping
from threading import Event, Timer
from typing import Generic, TypeVar

from connectrpc.code import Code
from connectrpc.errors import ConnectError
from connectrpc.protocol import ProtocolType
from connectrpc.request import Headers
from pyqwest import SyncClient, SyncRequest, SyncResponse

from chalkcompute._gen.chalk.sandbox.v2.process_service_connect import (
    SandboxProcessServiceClientSync,
)
from chalkcompute._gen.chalk.sandbox.v2.process_service_pb import (
    OpenSessionRequest,
    OpenSessionResponse,
)
from chalkcompute._http import _SYNC_HTTP_TRANSPORT
from chalkcompute._rpc import RPC_READ_MAX_BYTES, rpc_interceptors

_Result = TypeVar("_Result")
_Response = TypeVar("_Response")
FINAL_STATUS_TIMEOUT_SECONDS = 30.0


class ExecStreamError(ConnectError, Generic[_Result]):
    """An exec RPC error with its known command result."""

    def __init__(self, error: ConnectError, result: _Result) -> None:
        super().__init__(error.code, error.message, error.details)
        self.result = result


class _ExecCancellation:
    def __init__(self) -> None:
        self.response: SyncResponse | None = None

    def execute_sync(self, request: SyncRequest) -> SyncResponse:
        self.response = _SYNC_HTTP_TRANSPORT.execute_sync(request)
        return self.response

    def cancel(self) -> None:
        if self.response is not None:
            self.response.close()


class FinalStatusStream(Iterator[_Response]):
    def __init__(
        self, responses: Iterator[_Response], cancellation: _ExecCancellation
    ) -> None:
        self._responses = responses
        self._cancellation = cancellation
        self._timer: Timer | None = None
        self._expired = Event()

    def __iter__(self) -> Iterator[_Response]:
        return self

    def __next__(self) -> _Response:
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

    def start_timeout(self) -> None:
        if self._timer is not None:
            return

        def expire() -> None:
            self._expired.set()
            # Closing the HTTP body interrupts next(); closing its Python generator cannot.
            self._cancellation.cancel()

        self._timer = Timer(FINAL_STATUS_TIMEOUT_SECONDS, expire)
        self._timer.daemon = True
        self._timer.start()


class ExecProcessClient(SandboxProcessServiceClientSync):
    def open_session(
        self,
        request: Iterator[OpenSessionRequest],
        *,
        headers: Headers | Mapping[str, str] | None = None,
        timeout_ms: int | None = None,
    ) -> Iterator[OpenSessionResponse]:
        cancellation = _ExecCancellation()
        # Each stream owns cancellation; all transports share the connection pool.
        client = SandboxProcessServiceClientSync(
            self._address,
            protocol=ProtocolType.GRPC,
            interceptors=rpc_interceptors(),
            http_client=SyncClient(transport=cancellation),
            read_max_bytes=RPC_READ_MAX_BYTES,
        )
        return FinalStatusStream(
            client.open_session(request, headers=headers, timeout_ms=timeout_ms),
            cancellation,
        )


def start_final_status_timeout(responses: Iterator[object]) -> None:
    if isinstance(responses, FinalStatusStream):
        responses.start_timeout()
