"""Local transport checks for the Python binding of the shared Rust retry engine."""

from concurrent.futures import ThreadPoolExecutor
import inspect
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from protobuf import Oneof

from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as catalog_pb
from chalkcompute._gen.chalk.runtime.v1 import remote_python_call_pb as call_pb
from chalkcompute._retry import RetryPolicy


@pytest.fixture
def native():
    module = pytest.importorskip("chalkcompute._native")
    if (
        "retry_policy"
        not in inspect.signature(module.RemoteCallClient.call_ipc).parameters
    ):
        pytest.skip("Rebuild the native extension to test retry options")
    return module


@pytest.mark.parametrize(
    "retries,delay,timeout,expected_code,attempts",
    [
        (0, 0, 2, 2, 1),
        (2, 0, 2, 2, 3),
        (2, 2, 0.05, None, 1),
    ],
)
def test_native_direct_retries_preserve_payload_and_terminal_error(
    native, retries, delay, timeout, expected_code, attempts
):
    grpc = pytest.importorskip("grpc")
    payloads = []
    error = json.dumps(
        {
            "chalk_remote_error": 1,
            "exception": {"kind": "ValueError", "message": "failed"},
        }
    )

    def handler(requests, context):
        payloads.append(next(requests).feather_stream)
        context.abort(grpc.StatusCode.UNKNOWN, error)
        yield  # Keep this a streaming handler.

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "chalk.runtime.v1.RemoteCallService",
                {
                    "CallFunction": grpc.stream_stream_rpc_method_handler(
                        handler,
                        request_deserializer=call_pb.CallFunctionRequest.from_binary,
                        response_serializer=lambda response: response.to_binary(),
                    )
                },
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        client = native.RemoteCallClient(f"127.0.0.1:{port}", use_tls=False)
        expected_error = (
            TimeoutError if expected_code is None else native.RemoteCallStatusError
        )
        with pytest.raises(expected_error) as caught:
            client.call_ipc(
                "fn",
                b"same-input",
                retry_policy=RetryPolicy.linear(
                    attempts=retries, delay=delay
                ).to_dict(),
                timeout=timeout,
            )
        if expected_code is not None:
            assert caught.value.args[0] == expected_code
        if expected_code == 2:
            assert caught.value.args[1] == error
        assert payloads == [b"same-input"] * attempts
    finally:
        server.stop(0).wait()


def test_native_metadata_api_retries_preserve_version_and_headers(native):
    payloads = []
    headers = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payloads.append(self.rfile.read(int(self.headers["content-length"])))
            headers.append(dict(self.headers))
            if len(payloads) < 3:
                response = json.dumps(
                    {"code": "internal", "message": "temporary handler failure"}
                ).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
            else:
                response = catalog_pb.CallExternalFunctionResponse(
                    body=Oneof(
                        "remote_call_response",
                        call_pb.CallFunctionResponse(feather_stream=b"result"),
                    )
                ).to_binary()
                self.send_response(200)
                self.send_header("Content-Type", "application/proto")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        client = native.RemoteCallClient(
            f"http://127.0.0.1:{server.server_port}", api_mode=True
        )
        assert client.call_external_ipc(
            "fn",
            7,
            b"request",
            [("x-chalk-env-id", "test-env")],
            retry_policy=RetryPolicy.linear(attempts=2, delay=0).to_dict(),
            timeout=2,
        ) == [b"result"]
        assert len(payloads) == 3
        assert payloads[0] == payloads[1] == payloads[2]
        request = catalog_pb.CallExternalFunctionRequest.from_binary(payloads[0])
        assert request.function.version == 7
        assert all(
            next(
                value for key, value in item.items() if key.lower() == "x-chalk-env-id"
            )
            == "test-env"
            for item in headers
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_native_enqueue_preserves_override_presence(native):
    grpc = pytest.importorskip("grpc")
    requests = []

    def handler(request, context):
        requests.append(request)
        return call_pb.EnqueueRemoteCallResponse(call_id="stable-call")

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "chalk.runtime.v1.AsyncRemoteCallService",
                {
                    "EnqueueRemoteCall": grpc.unary_unary_rpc_method_handler(
                        handler,
                        request_deserializer=call_pb.EnqueueRemoteCallRequest.from_binary,
                        response_serializer=lambda response: response.to_binary(),
                    )
                },
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        client = native.RemoteCallClient(f"127.0.0.1:{port}", use_tls=False)
        assert client.enqueue("fn", b"request", max_retries=0) == "stable-call"
        assert client.enqueue("fn", b"request") == "stable-call"
        assert requests[0].has_field("max_retries")
        assert requests[0].max_retries == 0
        assert not requests[1].has_field("max_retries")
    finally:
        server.stop(0).wait()
