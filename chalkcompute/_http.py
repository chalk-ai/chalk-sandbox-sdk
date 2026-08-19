"""Shared synchronous HTTP transport for SDK and ConnectRPC requests."""

from pyqwest import (
    ReadError,
    RemoteProtocolError,
    SyncClient,
    TooManyRedirects,
    WriteError,
)


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
