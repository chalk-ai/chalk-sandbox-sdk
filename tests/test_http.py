"""Tests for the shared pyqwest HTTP transport."""

from pyqwest import HTTPVersion, SyncClient, SyncHTTPTransport

from chalkcompute import _http
from chalkcompute._rpc import RPC_READ_MAX_BYTES


def test_sync_http_client_pins_http2() -> None:
    assert _http.SYNC_HTTP_VERSION is HTTPVersion.HTTP2
    assert _http.TCP_KEEPALIVE_INTERVAL_SECONDS == 60.0
    assert isinstance(_http.SYNC_HTTP_CLIENT, SyncClient)
    assert isinstance(_http.SYNC_UNTRACED_HTTP_CLIENT, SyncClient)
    assert isinstance(_http._SYNC_HTTP_TRANSPORT, SyncHTTPTransport)
    assert isinstance(_http._SYNC_UNTRACED_HTTP_TRANSPORT, SyncHTTPTransport)


def test_rpc_read_max_bytes_is_100mb() -> None:
    assert RPC_READ_MAX_BYTES == 100 << 20
