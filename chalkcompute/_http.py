"""Shared synchronous HTTP transport for SDK and ConnectRPC requests."""

from collections.abc import Iterable, Iterator, Sequence
from typing import TYPE_CHECKING, Protocol, TypeVar, overload

from pyqwest import (
    HTTPVersion,
    ReadError,
    RemoteProtocolError,
    SyncClient,
    SyncHTTPTransport,
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


# Pin HTTP/2: ConnectRPC stubs always use ProtocolType.GRPC, which requires h2.
# Leaving http_version unset relies on ALPN and can fall back to HTTP/1.1 through
# proxies/LBs. Match pyqwest's default pool/timeout knobs and raise idle sockets
# so multiplexed RPCs keep a warmer connection pool.
#
# TCP keepalive at 60s approximates the old grpcio keepalive_time_ms=60_000.
# pyqwest has no HTTP/2 PING keepalive API.
SYNC_HTTP_VERSION = HTTPVersion.HTTP2
TCP_KEEPALIVE_INTERVAL_SECONDS = 60.0
_SYNC_HTTP_TRANSPORT = SyncHTTPTransport(
    http_version=SYNC_HTTP_VERSION,
    tls_include_system_certs=True,
    connect_timeout=30.0,
    pool_idle_timeout=90.0,
    tcp_keepalive_interval=TCP_KEEPALIVE_INTERVAL_SECONDS,
    pool_max_idle_per_host=32,
)
SYNC_HTTP_CLIENT = SyncClient(transport=_SYNC_HTTP_TRANSPORT)

# pyqwest deliberately uses built-in connection/timeout exceptions alongside
# its protocol and stream errors instead of defining one catch-all base class.
HTTP_TRANSPORT_ERRORS = (
    ConnectionError,
    ReadError,
    RemoteProtocolError,
    TooManyRedirects,
    WriteError,
)
