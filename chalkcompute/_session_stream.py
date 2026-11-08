"""Surface request-side errors on OpenSession bidi streams.

connectrpc encodes request messages inside the generator it hands to the
HTTP/2 transport as the request body. If encoding (or the request iterator)
raises there, hyper swallows the exception, resets the stream, and the caller
only sees ``ConnectError(UNAVAILABLE): ... stream error sent by user``. That
code also looks transient, so ``ExecProcess.wait`` would reattach and drop the
bad request. These wrappers catch the error in our own frame and re-raise it
from the response side instead.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TypeVar

from connectrpc.codec import proto_binary_codec
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf import Message

logger = logging.getLogger(__name__)

RPC_OPEN_SESSION = "chalk.sandbox.v2.SandboxProcessService/OpenSession"

ReqT = TypeVar("ReqT", bound=Message)
RespT = TypeVar("RespT")


@dataclass
class _StreamState:
    request_error: Exception | None = None


def open_session_stream(
    open_session: Callable[[Iterator[ReqT]], Iterable[RespT]],
    requests: Iterable[ReqT],
    *,
    sandbox_id: str,
    session_id: str | None = None,
    rpc: str = RPC_OPEN_SESSION,
) -> Iterator[RespT]:
    """Open a stream via ``open_session`` and return its response iterator.

    Errors raised while producing or encoding a request are re-raised from the
    response iterator in place of the transport's ``ConnectError``.
    """
    state = _StreamState()
    logger.debug(
        "%s stream opened (sandbox_id=%s, session_id=%s)", rpc, sandbox_id, session_id
    )
    responses = open_session(_checked_requests(requests, state, rpc))
    return _surfaced_responses(responses, state, rpc, sandbox_id, session_id)


def _checked_requests(
    requests: Iterable[ReqT], state: _StreamState, rpc: str
) -> Iterator[ReqT]:
    codec = proto_binary_codec()
    it = iter(requests)
    while True:
        try:
            request = next(it)
            # Encode here as well as in connectrpc so a failure raises in a
            # frame we can see. The double encode is cheap: control messages
            # are tiny and stdin chunks are a memcpy-scale encode.
            codec.encode(request)
        except StopIteration:
            return
        except Exception as exc:
            state.request_error = exc
            logger.error("%s request failed: %r", rpc, exc)
            raise
        yield request


def _surfaced_responses(
    responses: Iterable[RespT],
    state: _StreamState,
    rpc: str,
    sandbox_id: str,
    session_id: str | None,
) -> Iterator[RespT]:
    try:
        yield from responses
    except ConnectRpcError as exc:
        if state.request_error is not None:
            raise state.request_error from exc
        raise
    finally:
        logger.debug(
            "%s stream ended (sandbox_id=%s, session_id=%s)",
            rpc,
            sandbox_id,
            session_id,
        )
