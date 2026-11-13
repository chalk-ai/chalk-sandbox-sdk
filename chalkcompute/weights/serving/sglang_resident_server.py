"""Official SGLang HTTP server with a private, serialized scheduler IPC bridge."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from typing import Any

from chalkcompute.weights.serving.sglang_resident_worker import (
    MAX_CONTROL_BYTES,
    MAX_ACK_BYTES,
    METHOD,
    check_server_args,
    run_scheduler_process,
    validate_control,
)

CONTROL_PATH = "/chalk/weights/resident/control"


class SchedulerChannel:
    """One socket/lock; uncertain send/receive permanently poisons the channel."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.lock = asyncio.Lock()
        self.socket: Any = None
        self.context: Any = None
        self.poisoned = False

    async def call(self, parameters: dict[str, Any]) -> dict[str, Any]:
        validate_control(parameters)
        async with self.lock:
            if self.poisoned:
                raise RuntimeError("Uncertain scheduler channel; restart required")
            try:
                async with asyncio.timeout(600):
                    result = await self.exchange(parameters)
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise RuntimeError("Native sparse control refused")
                return result
            except BaseException:
                self.poisoned = True
                if self.socket is not None:
                    self.socket.close(linger=0)
                if self.context is not None:
                    self.context.term()
                raise

    async def exchange(self, parameters: dict[str, Any]) -> dict[str, Any]:
        # These optional runtime packages are absent from normal SDK deps.
        zmq = importlib.import_module("zmq")
        zmq_asyncio = importlib.import_module("zmq.asyncio")
        io = importlib.import_module("sglang.srt.managers.io_struct")
        network = importlib.import_module("sglang.srt.utils.network")

        if self.socket is None:
            self.context = zmq_asyncio.Context()
            # Matches Engine's DEALER bind and scheduler's existing DEALER connect.
            self.socket = network.get_zmq_socket(
                self.context, zmq.DEALER, self.endpoint, True
            )
            self.socket.setsockopt(zmq.LINGER, 0)
            self.socket.setsockopt(zmq.MAXMSGSIZE, MAX_CONTROL_BYTES)
            self.socket.setsockopt(zmq.RCVHWM, 1)
            self.socket.setsockopt(zmq.SNDHWM, 1)
        await io.async_sock_send(
            self.socket, io.RpcReqInput(method=METHOD, parameters=parameters)
        )
        result = await io.async_sock_recv(self.socket)
        if (
            not isinstance(result, io.RpcReqOutput)
            or result.success is not True
            or len(result.message.encode()) > MAX_ACK_BYTES
        ):
            raise RuntimeError("Invalid native sparse control acknowledgement")
        return json.loads(result.message)


def install_route(app: Any, channel: SchedulerChannel) -> None:
    fastapi = importlib.import_module("fastapi")
    HTTPException = fastapi.HTTPException

    async def endpoint(request: Any) -> dict[str, Any]:
        if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(status_code=403, detail="Loopback control required")
        body = bytearray()
        async for piece in request.stream():
            if len(body) + len(piece) > MAX_CONTROL_BYTES:
                raise HTTPException(
                    status_code=413, detail="Sparse control request too large"
                )
            body.extend(piece)
        try:
            parameters = json.loads(body)
            validate_control(parameters)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="Invalid sparse control request"
            ) from None
        try:
            return await channel.call(parameters)
        except Exception:
            raise HTTPException(
                status_code=503, detail="Sparse control failed; restart required"
            ) from None

    endpoint.__annotations__["request"] = fastapi.Request
    app.add_api_route(CONTROL_PATH, endpoint, methods=["POST"])


def init_tokenizer_manager(*args: Any, **kwargs: Any) -> Any:
    http_server = importlib.import_module("sglang.srt.entrypoints.http_server")
    native = importlib.import_module(
        "sglang.srt.entrypoints.engine"
    ).init_tokenizer_manager

    server_args = args[0] if args else kwargs["server_args"]
    port_args = args[1] if len(args) > 1 else kwargs["port_args"]
    if server_args.host != "127.0.0.1" or server_args.tokenizer_worker_num != 1:
        raise ValueError("Single loopback HTTP tokenizer required")
    install_route(http_server.app, SchedulerChannel(port_args.rpc_ipc_name))
    return native(*args, **kwargs)


def main() -> None:
    from chalkcompute.weights.serving.resident_runtime import check_runtime_sources

    check_runtime_sources("sglang")
    launch_server = importlib.import_module(
        "sglang.srt.entrypoints.http_server"
    ).launch_server
    prepare_server_args = importlib.import_module(
        "sglang.srt.server_args"
    ).prepare_server_args

    from chalkcompute.weights.serving.adapters.sglang_resident import (
        validate_environment,
    )

    validate_environment()
    server_args = prepare_server_args(sys.argv[1:])
    check_server_args(server_args)
    launch_server(
        server_args,
        init_tokenizer_manager_func=init_tokenizer_manager,
        run_scheduler_process_func=run_scheduler_process,
    )


if __name__ == "__main__":
    main()
