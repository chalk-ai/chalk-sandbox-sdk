"""Cross-repository CPU proof: real native gRPC admission and SDK update drain.

This tests the execution boundary with an instrumented handler, not GPU weights.
Run in a bounded subprocess with the candidate native extension and SDK on PYTHONPATH.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone

import grpc
import pyarrow as pa
import pytest

from chalk_remote_call import ServingHandle, serve
from chalk_remote_call._gen.chalk.runtime.v1 import remote_python_call_pb2 as pb
from chalk_remote_call._gen.chalk.runtime.v1.remote_python_call_pb2_grpc import (
    RemoteCallServiceStub,
)
from chalk_remote_call.arrow_utils import decode_ipc_stream, encode_record_batch
from chalkcompute.weights.serving.managed_target import (
    ManagedWeightTarget,
    target_fingerprint,
)
from chalkcompute.weights.serving.supervisor import AdmissionGate


def test_real_native_gate_drains_before_sdk_update_and_enforces_new_target():
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as listener:
        listener.bind(("::1", 0))
        port = listener.getsockname()[1]
    handle = ServingHandle(require_target=True)
    gate = AdmissionGate(handle)
    gate.managed_target = True
    started, release, changed = threading.Event(), threading.Event(), threading.Event()
    installed = {"version": 0}

    def handler(event, context):
        value = installed["version"]
        if event["x"][0].as_py() == 1:
            started.set()
            if not release.wait(5):
                raise TimeoutError("test handler release deadline")
        return pa.array([value])

    server = threading.Thread(
        target=serve,
        kwargs={
            "handler": handler,
            "host": "[::1]",
            "port": port,
            "workers": 2,
            "serving_handle": handle,
            "require_weight_fence": True,
        },
        daemon=True,
    )
    server.start()
    channel = grpc.insecure_channel(f"localhost:{port}")
    grpc.channel_ready_future(channel).result(timeout=5)
    client = RemoteCallServiceStub(channel)
    a = ManagedWeightTarget(
        1,
        "deployment-a",
        "revision-a",
        1,
        "stream-a",
        0,
        "volume-a",
        42,
        "a" * 64,
        "b" * 64,
        datetime.fromtimestamp(time.time() + 30, timezone.utc),
    )
    c = replace(
        a,
        target_generation=2,
        logical_version=2,
        volume_version_id=44,
        manifest_sha256="c" * 64,
        lease_expires_at=datetime.fromtimestamp(time.time() + 31, timezone.utc),
    )

    def invoke(target=None, blocking=False):
        data = encode_record_batch(
            pa.record_batch([pa.array([1 if blocking else 0])], names=["x"])
        )
        metadata = [] if target is None else [("x-chalk-model-weight-target", target)]
        results = list(
            client.CallFunction(
                iter([pb.CallFunctionRequest(name="handler", feather_stream=data)]),
                metadata=metadata,
                timeout=5,
            )
        )
        return decode_ipc_stream(results[0].feather_stream)[0].column(0).to_pylist()

    def rejected(target=None):
        with pytest.raises(grpc.RpcError) as exc:
            invoke(target)
        assert exc.value.code() == grpc.StatusCode.UNAVAILABLE

    try:
        rejected(target_fingerprint(a.observation()))
        gate.version = 0
        gate.mark_applied_target(a)
        assert (
            target_fingerprint(a.observation())
            == "ae795dd3af9b52d686199fd8df596b9f860d9d0352843b1d5055ee3c45d5efd7"
        )
        rejected()
        rejected("f" * 64)
        assert invoke(target_fingerprint(a.observation())) == [0]
        result, errors = [], []

        def active_call():
            try:
                result.extend(
                    invoke(target_fingerprint(a.observation()), blocking=True)
                )
            except BaseException as error:
                errors.append(error)

        active = threading.Thread(target=active_call)
        active.start()
        assert started.wait(3)

        async def update():
            async def perform():
                async with gate.update(3, target=c):
                    changed.set()
                    installed["version"] = 2
                    gate.version = 2
                    gate.mark_applied_target(c)

            task = asyncio.create_task(perform())
            deadline = time.monotonic() + 2
            while handle.is_serving and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert not handle.is_serving
            await asyncio.sleep(0.05)
            assert not changed.is_set(), (
                "weights changed while native handler was still executing"
            )
            assert installed["version"] == 0
            await asyncio.to_thread(rejected, target_fingerprint(c.observation()))
            release.set()
            await task

        asyncio.run(update())
        active.join(3)
        assert not active.is_alive()
        assert not errors
        assert result == [0]
        assert changed.is_set()
        rejected(target_fingerprint(a.observation()))
        rejected()
        assert invoke(target_fingerprint(c.observation())) == [2]
        c_next = replace(
            c,
            target_generation=3,
            lease_expires_at=datetime.fromtimestamp(time.time() + 32, timezone.utc),
        )

        async def same_version_selection():
            async with gate.update(2, target=c_next):
                gate.mark_applied_target(c_next)

        asyncio.run(same_version_selection())
        rejected(target_fingerprint(c.observation()))
        assert invoke(target_fingerprint(c_next.observation())) == [2]
    finally:
        release.set()
        handle.set_serving(False)
        assert handle.wait_drained(3)
        channel.close()
