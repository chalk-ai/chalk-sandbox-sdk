"""Exercise HTTP/2 final status with explicitly controlled timeout callbacks."""

from concurrent.futures import ThreadPoolExecutor
import queue
import threading

import grpc
import pytest
from connectrpc.code import Code
from protobuf import Oneof

from chalkcompute import ExecStreamError, _exec
from chalkcompute._gen.chalk.sandbox.v2 import process_service_pb as pb
from chalkcompute._sandbox import ExecProcess, SandboxClient


@pytest.fixture
def timers(monkeypatch):
    started = queue.Queue()

    class ManualTimer:
        def __init__(self, interval, callback):
            self.interval = interval
            self.callback = callback
            self.cancelled = False

        def start(self):
            started.put(self)

        def cancel(self):
            self.cancelled = True

        def fire(self):
            assert not self.cancelled
            self.callback()

    monkeypatch.setattr(_exec, "Timer", ManualTimer)
    return started


@pytest.fixture
def process_rpc(timers):
    resources = []

    def start(exit_code=7, final_status=None, *, terminal=True, exit_gate=None):
        closed, release = threading.Event(), threading.Event()
        pool = ThreadPoolExecutor(max_workers=2)
        server = grpc.server(pool)
        calls = []

        def handle(requests, context):
            calls.append(True)
            context.add_callback(closed.set)
            context.add_callback(release.set)
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
                if exit_gate is not None:
                    exit_gate[0].set()
                    exit_gate[1].wait()
                yield pb.OpenSessionResponse(
                    event=Oneof("process_exited", pb.ProcessExited(exit_code=exit_code))
                ).to_binary()
            if final_status == "hung":
                release.wait()
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
        resources.append((server, pool, client, release, requests, exit_gate))
        return (
            ExecProcess(requests, responses, "sandbox", client),
            closed,
            calls,
            release,
        )

    yield start
    for server, pool, client, release, requests, exit_gate in resources:
        requests.put(None)
        release.set()
        if exit_gate is not None:
            exit_gate[1].set()
        server.stop(0).wait()
        pool.shutdown()
        client.close()


@pytest.mark.parametrize("method", ["wait", "output"])
@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize("status", [None, grpc.StatusCode.DATA_LOSS, "hung"])
def test_final_status(process_rpc, timers, method, exit_code, status):
    process, closed, calls, release = process_rpc(exit_code, status)

    def consume():
        if method == "wait":
            return process.wait()
        events = []
        for event in process.output():
            if status is not None:
                assert not event.is_exited
            events.append(event)
        return events[-1]

    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(consume)
        try:
            if status == "hung":
                timer = timers.get(timeout=5)
                assert timer.interval == 30
                timer.fire()
                assert closed.wait(5), "the timer must cancel the actual HTTP/2 stream"
            if status is None:
                result = future.result(timeout=5)
                assert result.exit_code == exit_code
                if method == "wait":
                    assert result.stdout == b"out\x00\xff"
            else:
                with pytest.raises(ExecStreamError) as caught:
                    future.result(timeout=5)
                assert caught.value.code == (
                    Code.DEADLINE_EXCEEDED if status == "hung" else Code.DATA_LOSS
                )
                assert caught.value.result.exit_code == exit_code
                if method == "wait":
                    assert caught.value.result.stdout == b"out\x00\xff"
                assert closed.wait(5)
            if status != "hung":
                assert timers.get(timeout=5).cancelled
            assert len(calls) == 1
        finally:
            release.set()


def test_data_loss_before_exit_is_not_retried(process_rpc, timers):
    process, _, calls, _ = process_rpc(
        final_status=grpc.StatusCode.DATA_LOSS, terminal=False
    )
    with pytest.raises(ExecStreamError) as caught:
        process.wait()
    assert caught.value.code == Code.DATA_LOSS
    assert caught.value.result.exit_code is None
    assert len(calls) == 1
    assert timers.empty()


def test_timeout_does_not_cancel_another_stream(process_rpc, timers):
    exit_ready, allow_exit = threading.Event(), threading.Event()
    hung, hung_closed, _, release = process_rpc(final_status="hung")
    running, running_closed, _, _ = process_rpc(exit_gate=(exit_ready, allow_exit))
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(hung.wait)
        second = workers.submit(running.wait)
        try:
            assert exit_ready.wait(5)
            timer = timers.get(timeout=5)
            assert timer.interval == 30
            assert timers.empty(), "a running process must not arm a final-status timer"
            timer.fire()
            with pytest.raises(ExecStreamError) as caught:
                first.result(timeout=5)
            assert caught.value.code == Code.DEADLINE_EXCEEDED
            assert hung_closed.wait(5)
            assert not running_closed.is_set()
            assert not second.done()
            allow_exit.set()
            assert second.result(timeout=5).exit_code == 7
            assert timers.get(timeout=5).cancelled
        finally:
            release.set()
            allow_exit.set()
