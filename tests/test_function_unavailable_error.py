from concurrent.futures import ThreadPoolExecutor
import socket

import pytest

import chalkcompute as cc
from chalkcompute import _remote_call_client
from chalkcompute._gen.chalk.runtime.v1 import remote_python_call_pb as call_pb
from chalkcompute._retry import RetryPolicy


def test_function_unavailable_error_is_separate_from_execution_errors() -> None:
    assert issubclass(cc.FunctionUnavailableError, RuntimeError)
    assert not issubclass(cc.FunctionUnavailableError, cc.FunctionError)
    assert not issubclass(cc.RemoteExecutionError, cc.FunctionUnavailableError)
    for error in (
        _remote_call_client.RemoteCallUnavailableError,
        _remote_call_client.RemoteCallTransportError,
        _remote_call_client._RemoteCallUnavailableError,
        _remote_call_client._RemoteCallTransportError,
    ):
        assert issubclass(error, cc.FunctionUnavailableError)
        assert issubclass(error, RuntimeError)
    for error in (
        _remote_call_client.RemoteCallStatusError,
        _remote_call_client.RemoteCallNotFoundError,
    ):
        assert not issubclass(error, cc.FunctionUnavailableError)


@pytest.fixture
def native():
    return pytest.importorskip("chalkcompute._native")


def test_native_errors_derive_from_the_public_type(native) -> None:
    assert issubclass(native.RemoteCallUnavailableError, cc.FunctionUnavailableError)
    assert issubclass(native.RemoteCallTransportError, cc.FunctionUnavailableError)
    assert not issubclass(native.RemoteCallStatusError, cc.FunctionUnavailableError)


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("UNAVAILABLE", "RemoteCallUnavailableError"),
        ("UNKNOWN", "RemoteCallStatusError"),
    ],
)
def test_native_call_status_maps_to_function_unavailable_error(
    native, code: str, error: str
) -> None:
    grpc = pytest.importorskip("grpc")

    def handler(requests, context):
        next(requests)
        context.abort(getattr(grpc.StatusCode, code), "no healthy upstream")
        yield

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
        with pytest.raises(getattr(native, error)) as caught:
            client.call_ipc(
                "fn", b"input", retry_policy=RetryPolicy.linear(attempts=0).to_dict()
            )
        assert isinstance(caught.value, cc.FunctionUnavailableError) is (
            code == "UNAVAILABLE"
        )
    finally:
        server.stop(0).wait()


def test_native_connection_refused_is_function_unavailable_error(native) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(cc.FunctionUnavailableError):
        native.RemoteCallClient(f"127.0.0.1:{port}", use_tls=False)
