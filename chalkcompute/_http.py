"""Shared synchronous HTTP transport for SDK and ConnectRPC requests."""

from typing import Protocol, TypeVar

from pyqwest import (
    Headers,
    ReadError,
    RemoteProtocolError,
    SyncClient,
    TooManyRedirects,
    WriteError,
)


_ContentT_co = TypeVar("_ContentT_co", covariant=True)


class HTTPResponse(Protocol[_ContentT_co]):
    """Structural response interface shared by buffered and streaming clients.

    ``pyqwest.FullResponse`` implements ``HTTPResponse[bytes]`` and
    ``pyqwest.SyncResponse`` implements the same protocol with an iterator as
    its content type. No response wrapper or conversion is required.
    """

    @property
    def status(self) -> int: ...

    @property
    def headers(self) -> Headers: ...

    @property
    def content(self) -> _ContentT_co: ...

    @property
    def trailers(self) -> Headers: ...


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
