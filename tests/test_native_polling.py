"""Local transport coverage for deferred-result polling."""

from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from chalkcompute._gen.chalk.runtime.v1 import remote_python_call_pb as call_pb
from chalkcompute._remote_call_client import RemoteCallClient


def test_default_poll_backoff_reads_short_lived_result() -> None:
    pytest.importorskip("chalkcompute._native")
    grpc = pytest.importorskip("grpc")
    expires_at: float | None = None

    def poll(request, context):
        nonlocal expires_at
        if expires_at is None:
            # The result becomes available just after an empty poll. Four
            # seconds leaves scheduling margin locally, but the old five-second
            # polling cap missed this result entirely.
            expires_at = time.monotonic() + 4
            status = call_pb.RemoteCallStatus.PENDING
        elif time.monotonic() < expires_at:
            status = call_pb.RemoteCallStatus.COMPLETED
        else:
            status = call_pb.RemoteCallStatus.EXPIRED
        return call_pb.PollRemoteCallResponse(status=status)

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "chalk.runtime.v1.AsyncRemoteCallService",
                {
                    "PollRemoteCall": grpc.unary_unary_rpc_method_handler(
                        poll,
                        request_deserializer=call_pb.PollRemoteCallRequest.from_binary,
                        response_serializer=lambda response: response.to_binary(),
                    )
                },
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        client = RemoteCallClient(f"127.0.0.1:{port}", use_tls=False)
        try:
            # Exercise the capped delay without waiting through the ramp-up.
            pending = client.poll_next("call", poll_count=100)
            assert pending.status_name == "pending"
            completed = client.poll_next("call", pending.cursor, pending.poll_count)
            assert completed.is_completed
        finally:
            client.close()
    finally:
        server.stop(0).wait()
