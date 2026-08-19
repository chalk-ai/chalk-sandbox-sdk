"""Shared synchronous HTTP transport for SDK and ConnectRPC requests."""

from collections.abc import Iterable, Iterator, Sequence
from typing import TYPE_CHECKING, Protocol, TypeVar, overload

from pyqwest import (
    ReadError,
    RemoteProtocolError,
    SyncClient,
    TooManyRedirects,
    WriteError,
)


_ContentT_co = TypeVar("_ContentT_co", covariant=True)
_HeaderDefaultT = TypeVar("_HeaderDefaultT")


class HTTPHeaders(Protocol):
    """Transport-independent, read-only HTTP header interface."""

    def __getitem__(self, name: str, /) -> str: ...

    def __contains__(self, name: object, /) -> bool: ...

    def __iter__(self) -> Iterator[str]: ...

    def __len__(self) -> int: ...

    @overload
    def get(self, name: str, /) -> str | None: ...

    @overload
    def get(self, name: str, default: _HeaderDefaultT, /) -> str | _HeaderDefaultT: ...

    def getall(self, name: str, /) -> Sequence[str]: ...

    def items(self) -> Iterable[tuple[str, str]]: ...

    def keys(self) -> Iterable[str]: ...

    def values(self) -> Iterable[str]: ...


class HTTPResponse(Protocol[_ContentT_co]):
    """Transport-independent response interface.

    Concrete transport responses implement this protocol structurally, without
    requiring a response wrapper or conversion.
    """

    @property
    def status(self) -> int: ...

    @property
    def headers(self) -> HTTPHeaders: ...

    @property
    def content(self) -> _ContentT_co: ...

    @property
    def trailers(self) -> HTTPHeaders: ...


if TYPE_CHECKING:
    from pyqwest import FullResponse, SyncResponse

    def _verify_response_protocols(
        buffered: FullResponse,
        streaming: SyncResponse,
    ) -> None:
        buffered_response: HTTPResponse[bytes] = buffered
        streaming_response: HTTPResponse[Iterator[bytes | bytearray | memoryview]] = (
            streaming
        )
        del buffered_response, streaming_response


SYNC_HTTP_CLIENT = SyncClient()

# pyqwest deliberately uses built-in connection/timeout exceptions alongside
# its protocol and stream errors instead of defining one catch-all base class.
HTTP_TRANSPORT_ERRORS = (
    ConnectionError,
    ReadError,
    RemoteProtocolError,
    TooManyRedirects,
    WriteError,
)
