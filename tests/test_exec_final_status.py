"""Exercise final status and cancellation over actual HTTP/2 RPCs."""

from concurrent.futures import ThreadPoolExecutor
import queue
import threading
import time

import grpc
import pytest
from connectrpc.code import Code
from protobuf import Oneof

from chalkcompute import ExecStreamError, _exec
from chalkcompute._gen.chalk.sandbox.v2 import process_service_pb as pb
from chalkcompute._sandbox import ExecProcess, SandboxClient


@pytest.fixture
def process_rpc(monkeypatch):
    monkeypatch.setattr(_exec, "FINAL_STATUS_TIMEOUT_SECONDS", 0.1)
    resources = []

    def start(exit_code=7, final_status=None, *, terminal=True, before_exit=0.15):
        closed, release = threading.Event(), threading.Event()
        pool = ThreadPoolExecutor(max_workers=2)
        server = grpc.server(pool)
        calls = []

        def handle(requests, context):
            calls.append(True)
            context.add_callback(closed.set)
            yield pb.OpenSessionResponse(
                event=Oneof("session_attached", pb.SessionAttached(session_id="s"))
            ).to_binary()
            yield pb.OpenSessionResponse(
                event=Oneof(
                    "output_data",
                    pb.OutputData(
                        stream=pb.OutputData.Stream.STDOUT, data=b"out\x00\xff"
                    ),
                )
            ).to_binary()
            if terminal:
                time.sleep(
                    before_exit
                )  # Running commands may exceed the final-status budget.
                yield pb.OpenSessionResponse(
                    event=Oneof("process_exited", pb.ProcessExited(exit_code=exit_code))
                ).to_binary()
            if final_status == "hung":
                release.wait(3)
            elif final_status is not None:
                context.set_code(final_status)
                context.set_details("late error")

        handler = grpc.stream_stream_rpc_method_handler(handle)
        server.add_generic_rpc_handlers(
            [
                grpc.method_handlers_generic_handler(
                    "chalk.sandbox.v2.SandboxProcessService", {"OpenSession": handler}
                )
            ]
        )
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        client = SandboxClient(f"127.0.0.1:{port}")
        requests = queue.Queue()
        requests.put(pb.OpenSessionRequest())

        def request_iterator():
            while (request := requests.get()) is not None:
                yield request

        responses = client._process_stub.open_session(request_iterator())
        resources.append((server, pool, client, release, requests))
        return ExecProcess(requests, responses, "sandbox", client), closed, calls

    yield start
    for server, pool, client, release, requests in resources:
        requests.put(None)
        release.set()
        server.stop(0).wait()
        pool.shutdown()
        client.close()


@pytest.mark.parametrize("method", ["wait", "output"])
@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize("status", [None, grpc.StatusCode.DATA_LOSS, "hung"])
def test_final_status(process_rpc, method, exit_code, status):
    process, closed, calls = process_rpc(exit_code, status)
    if status is None:
        if method == "wait":
            result = process.wait()
            assert result.stdout == b"out\x00\xff"
            assert result.exit_code == exit_code
        else:
            assert list(process.output())[-1].exit_code == exit_code
    else:
        with pytest.raises(ExecStreamError) as caught:
            if method == "wait":
                process.wait()
            else:
                for event in process.output():
                    assert not event.is_exited
        assert caught.value.code == (
            Code.DEADLINE_EXCEEDED if status == "hung" else Code.DATA_LOSS
        )
        assert caught.value.result.exit_code == exit_code
        if method == "wait":
            assert caught.value.result.stdout == b"out\x00\xff"
        assert closed.wait(1)
    assert len(calls) == 1


def test_data_loss_before_exit_is_not_retried(process_rpc):
    process, _, calls = process_rpc(
        final_status=grpc.StatusCode.DATA_LOSS, terminal=False
    )
    with pytest.raises(ExecStreamError) as caught:
        process.wait()
    assert caught.value.code == Code.DATA_LOSS
    assert caught.value.result.exit_code is None
    assert len(calls) == 1


def test_timeout_does_not_cancel_another_stream(process_rpc):
    hung, _, _ = process_rpc(final_status="hung")
    running, _, _ = process_rpc(before_exit=0.35)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(hung.wait)
        second = pool.submit(running.wait)
        with pytest.raises(ExecStreamError) as caught:
            first.result(timeout=2)
        assert caught.value.code == Code.DEADLINE_EXCEEDED
        assert second.result(timeout=2).exit_code == 7
