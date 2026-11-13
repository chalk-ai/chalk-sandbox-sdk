from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Protocol

from aiohttp import ClientError, ClientSession, ClientTimeout, hdrs, web
from chalkcompute.weights.serving.managed_target import ManagedWeightTarget

logger = logging.getLogger(__name__)


class UnhealthyError(RuntimeError):
    pass


class DrainTimeout(TimeoutError):
    """The update was abandoned without changing engine state."""


class NativeServingHandle(Protocol):
    def set_serving(
        self, serving: bool, lease_expires_at: float | None = None
    ) -> None: ...


@dataclass
class Metrics:
    apply_ms: float = 0
    pause_ms: float = 0
    bytes: int = 0
    density: float = 0
    lag_versions: int = 0
    updates: int = 0
    failures: int = 0

    def prometheus(self) -> str:
        lines = []
        for name, value in vars(self).items():
            kind = "counter" if name in {"updates", "failures"} else "gauge"
            key = f"chalk_weight_sync_{name}"
            lines.extend([f"# TYPE {key} {kind}", f"{key} {value}"])
        return "\n".join(lines) + "\n"


class AdmissionGate:
    """Linearize request admission with policy changes, including streaming bodies."""

    def __init__(self, serving_handle: NativeServingHandle | None = None) -> None:
        self.version: int | None = None
        self.healthy = True
        self.managed_target = False
        self.applied_target: dict[str, str | int] | None = None
        self.applied_lease_expires_at: float | None = None
        self.applied_lease_deadline: float | None = None
        self.serving_handle = serving_handle
        self.failed = asyncio.Event()
        self.failure_reason: str | None = None
        self._paused = False
        self._active = 0
        self._condition = asyncio.Condition()
        self._update_lock = asyncio.Lock()
        if self.serving_handle is not None:
            self.serving_handle.set_serving(False)

    @property
    def ready(self) -> bool:
        return (
            self.healthy
            and self.version is not None
            and (not self.managed_target or not self._paused)
            and (
                not self.managed_target
                or (
                    self.applied_target is not None
                    and self.applied_lease_deadline is not None
                    and time.monotonic() < self.applied_lease_deadline
                )
            )
        )

    @property
    def paused(self) -> bool:
        return self._paused

    def _close_native_admission(self) -> None:
        if self.serving_handle is not None:
            self.serving_handle.set_serving(False)

    def _open_native_admission(self) -> None:
        if (
            self.serving_handle is not None
            and self.applied_target is not None
            and self.applied_lease_expires_at is not None
            and self.applied_lease_deadline is not None
            and time.monotonic() < self.applied_lease_deadline
        ):
            self.serving_handle.set_serving(
                True, lease_expires_at=self.applied_lease_expires_at
            )

    def mark_applied_target(self, target: ManagedWeightTarget) -> None:
        expires_at = target.lease_expires_at.timestamp()
        if (
            self.applied_lease_expires_at is not None
            and expires_at < self.applied_lease_expires_at
        ):
            raise UnhealthyError("Managed target lease renewal moved backwards")
        remaining = expires_at - time.time()
        if remaining <= 0 or remaining > 300:
            raise UnhealthyError(
                "Managed target lease is expired or exceeds 300 seconds"
            )
        self.applied_target = target.observation()
        if expires_at > (self.applied_lease_expires_at or 0):
            self.applied_lease_expires_at = expires_at
            self.applied_lease_deadline = time.monotonic() + remaining
        self._open_native_admission()

    def renew_native_lease(self, target: ManagedWeightTarget) -> None:
        if (
            self.ready
            and self.applied_target is not None
            and self.applied_target.get("target_generation") == target.target_generation
        ):
            expires_at = target.lease_expires_at.timestamp()
            if (
                self.applied_lease_expires_at is not None
                and expires_at < self.applied_lease_expires_at
            ):
                raise UnhealthyError("Managed target lease renewal moved backwards")
            if expires_at > (self.applied_lease_expires_at or 0):
                remaining = expires_at - time.time()
                if remaining <= 0 or remaining > 300:
                    raise UnhealthyError(
                        "Managed target lease is expired or exceeds 300 seconds"
                    )
                self.applied_lease_expires_at = expires_at
                self.applied_lease_deadline = time.monotonic() + remaining
            self._open_native_admission()

    async def pause(self) -> None:
        async with self._condition:
            if not self.healthy:
                raise UnhealthyError("The live policy is unhealthy")
            self._paused = True
            self._close_native_admission()
            self._condition.notify_all()

    @contextlib.asynccontextmanager
    async def admit(self, *, context: str = "inference request") -> AsyncGenerator[int]:
        async with self._condition:
            await self._condition.wait_for(lambda: not self._paused or not self.healthy)
            if (
                self.managed_target
                and self.applied_lease_deadline is not None
                and time.monotonic() >= self.applied_lease_deadline
            ):
                self._fail_locked("Managed target lease expired")
            if not self.healthy or self.version is None:
                raise UnhealthyError("The live policy is not ready")
            if self.managed_target and (
                self.applied_target is None or self.applied_lease_deadline is None
            ):
                raise UnhealthyError("The managed target is not ready")
            version = self.version
            self._active += 1
        try:
            yield version
        except BaseException as exc:
            # Mark uncertainty before decrementing active: an update waiting on
            # the final request must never race uncertain native completion.
            await self.fail(f"{type(exc).__name__} during admitted {context}")
            raise
        finally:
            async with self._condition:
                self._active -= 1
                self._condition.notify_all()

    @contextlib.asynccontextmanager
    async def update(
        self, timeout: float, target: ManagedWeightTarget | None = None
    ) -> AsyncGenerator[None]:
        async with self._update_lock:
            async with self._condition:
                if not self.healthy:
                    raise UnhealthyError("The live policy is unhealthy")
                self._paused = True
                self._close_native_admission()
                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(
                            lambda: self._active == 0 or not self.healthy
                        ),
                        timeout,
                    )
                except TimeoutError as exc:
                    target_changed = (
                        target is not None
                        and self.applied_target is not None
                        and self.applied_target.get("target_generation")
                        != target.target_generation
                    )
                    if not target_changed:
                        self._paused = False
                        self._open_native_admission()
                    self._condition.notify_all()
                    raise DrainTimeout(
                        "Requests did not drain; weights unchanged"
                    ) from exc
                except BaseException:
                    self._paused = False
                    self._open_native_admission()
                    self._condition.notify_all()
                    raise
            completed = False
            try:
                if not self.healthy:
                    raise UnhealthyError("The live policy is unhealthy")
                yield
                completed = True
            finally:
                async with self._condition:
                    if target is None or completed:
                        self._paused = False
                    else:
                        self._paused = True
                        self._close_native_admission()
                    self._condition.notify_all()

    async def fail(self, reason: str | None = None) -> None:
        async with self._condition:
            self._fail_locked(reason)

    def _fail_locked(self, reason: str | None) -> None:
        if reason is not None and self.failure_reason is None:
            self.failure_reason = reason
            logger.error("Live policy failed closed: %s", reason)
        self.healthy = False
        self._close_native_admission()
        self.failed.set()
        self._condition.notify_all()


# Engine management is deliberately unreachable through the public proxy.
# Native vLLM exposes other development endpoints when DEV_MODE is enabled;
# admitting only OpenAI /v1 routes also blocks any future management routes.
_OPENAI_ROUTES = frozenset(
    {
        "/v1/models",
        "/v1/completions",
        "/v1/chat/completions",
        "/v1/embeddings",
        "/v1/responses",
        "/v1/audio/transcriptions",
        "/v1/audio/translations",
        "/v1/score",
        "/v1/rerank",
    }
)
_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def _headers(headers, *, response: bool = False) -> dict[str, str]:
    connection = {h.strip().lower() for h in headers.get("Connection", "").split(",")}
    excluded = _HOP_HEADERS | connection | {"host", "x-chalk-policy-version"}
    if response:
        excluded |= {"x-chalk-weight-version"}
    return {k: v for k, v in headers.items() if k.lower() not in excluded}


def create_app(
    gate: AdmissionGate,
    engine_url: str,
    metrics: Metrics | None = None,
    *,
    request_timeout: float | None = None,
) -> web.Application:
    metrics = metrics or Metrics()
    app = web.Application(
        client_max_size=0,
        handler_args={"handler_cancellation": True, "auto_decompress": False},
    )
    native_tasks: set[asyncio.Task] = set()

    async def clients(app: web.Application):
        async with ClientSession(
            timeout=ClientTimeout(total=request_timeout),
            auto_decompress=False,
            skip_auto_headers={hdrs.ACCEPT_ENCODING},
        ) as client:
            app[client_key] = client
            try:
                yield
            finally:
                # The serving entrypoint stops the engine process group before
                # app cleanup, so cancelling an uncertain drain cannot race an
                # engine weight write or leave native generation running.
                remaining = tuple(native_tasks)
                for task in remaining:
                    task.cancel()
                await asyncio.gather(*remaining, return_exceptions=True)

    client_key = web.AppKey("engine_client", ClientSession)
    app.cleanup_ctx.append(clients)

    async def health(request: web.Request) -> web.Response:
        ready = gate.ready
        headers = (
            {}
            if gate.version is None
            else {"x-chalk-weight-version": str(gate.version)}
        )
        body: dict[str, object] = {"applied_version": gate.version, "healthy": ready}
        if gate.managed_target:
            body["applied_target"] = gate.applied_target
        return web.json_response(
            body,
            status=200 if ready else 503,
            headers=headers,
        )

    async def prometheus(request: web.Request) -> web.Response:
        return web.Response(text=metrics.prometheus(), content_type="text/plain")

    async def proxy(request: web.Request) -> web.StreamResponse:
        if request.path not in _OPENAI_ROUTES:
            raise web.HTTPNotFound()
        detached = asyncio.Event()
        queue: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue(maxsize=8)
        metadata = asyncio.get_running_loop().create_future()

        async def enqueue(value: bytes | BaseException | None) -> None:
            if detached.is_set():
                return
            put = asyncio.create_task(queue.put(value))
            disconnected = asyncio.create_task(detached.wait())
            try:
                await asyncio.wait(
                    (put, disconnected), return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in (put, disconnected):
                    task.cancel()
                await asyncio.gather(put, disconnected, return_exceptions=True)

        async def consume_native() -> None:
            try:
                async with gate.admit(
                    context=f"{request.method} {request.path}"
                ) as version:
                    if detached.is_set():
                        # No native request was started while admission waited.
                        metadata.set_result(
                            UnhealthyError("Client disconnected before admission")
                        )
                        return
                    async with app[client_key].request(
                        request.method,
                        engine_url.rstrip("/") + str(request.rel_url),
                        headers=_headers(request.headers),
                        data=request.content.iter_any(),
                        allow_redirects=False,
                    ) as upstream:
                        headers = _headers(upstream.headers, response=True)
                        headers["x-chalk-policy-version"] = str(version)
                        metadata.set_result((upstream.status, headers))
                        async for chunk in upstream.content.iter_chunked(64 * 1024):
                            await enqueue(chunk)
                # Releasing admission requires native HTTP EOF, even if the
                # downstream client disappeared while generation was running.
            except asyncio.CancelledError as exc:
                detached.set()
                if not metadata.done():
                    metadata.set_result(exc)
                raise
            except BaseException as exc:
                if not metadata.done():
                    metadata.set_result(exc)
                await enqueue(exc)
            finally:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    # Cancellation can arrive while publishing an error into a
                    # full queue. Cleanup must never await the stopped consumer.
                    detached.set()
                await enqueue(None)

        native = asyncio.create_task(consume_native(), name="inference-native-drain")
        native_tasks.add(native)
        native.add_done_callback(native_tasks.discard)
        response: web.StreamResponse | None = None
        try:
            result = await asyncio.shield(metadata)
            if isinstance(result, BaseException):
                raise result
            status, headers = result
            response = web.StreamResponse(status=status, headers=headers)
            await response.prepare(request)
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                if isinstance(chunk, BaseException):
                    raise chunk
                await response.write(chunk)
            await response.write_eof()
            return response
        except UnhealthyError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from exc
        except (asyncio.CancelledError, ClientError, OSError, TimeoutError) as exc:
            # Native consumption owns admission and survives client cancellation.
            # Its own transport errors still fail the gate closed.
            logger.info(
                "Inference downstream detached: method=%s route=%s exception=%s",
                request.method,
                request.path,
                type(exc).__name__,
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            if response is not None and response.prepared:
                response.force_close()
                raise
            raise web.HTTPBadGateway(text="Inference engine unavailable") from exc
        finally:
            detached.set()

    app.router.add_get("/chalk/health", health)
    app.router.add_get("/chalk/metrics", prometheus)
    app.router.add_route("*", "/{path:.*}", proxy)
    return app
