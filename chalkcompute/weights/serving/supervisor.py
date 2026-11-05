from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession, ClientTimeout, hdrs, web


class UnhealthyError(RuntimeError):
    pass


class DrainTimeout(TimeoutError):
    """The update was abandoned without changing engine state."""


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

    def __init__(self) -> None:
        self.version: int | None = None
        self.healthy = True
        self.failed = asyncio.Event()
        self._paused = False
        self._active = 0
        self._condition = asyncio.Condition()
        self._update_lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def admit(self) -> AsyncGenerator[int]:
        async with self._condition:
            await self._condition.wait_for(lambda: not self._paused or not self.healthy)
            if not self.healthy or self.version is None:
                raise UnhealthyError("The live policy is not ready")
            version = self.version
            self._active += 1
        try:
            yield version
        except BaseException:
            # Mark uncertainty before decrementing active: an update waiting on
            # the final request must never race a disconnected engine request.
            await self.fail()
            raise
        finally:
            async with self._condition:
                self._active -= 1
                self._condition.notify_all()

    @contextlib.asynccontextmanager
    async def update(self, timeout: float) -> AsyncGenerator[None]:
        async with self._update_lock:
            async with self._condition:
                if not self.healthy:
                    raise UnhealthyError("The live policy is unhealthy")
                self._paused = True
                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(
                            lambda: self._active == 0 or not self.healthy
                        ),
                        timeout,
                    )
                except TimeoutError as exc:
                    self._paused = False
                    self._condition.notify_all()
                    raise DrainTimeout(
                        "Requests did not drain; weights unchanged"
                    ) from exc
                except BaseException:
                    self._paused = False
                    self._condition.notify_all()
                    raise
            try:
                if not self.healthy:
                    raise UnhealthyError("The live policy is unhealthy")
                yield
            finally:
                async with self._condition:
                    self._paused = False
                    self._condition.notify_all()

    async def fail(self) -> None:
        async with self._condition:
            self.healthy = False
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

    async def clients(app: web.Application):
        async with ClientSession(
            timeout=ClientTimeout(total=request_timeout),
            auto_decompress=False,
            skip_auto_headers={hdrs.ACCEPT_ENCODING},
        ) as client:
            app[client_key] = client
            yield

    client_key = web.AppKey("engine_client", ClientSession)
    app.cleanup_ctx.append(clients)

    async def health(request: web.Request) -> web.Response:
        ready = gate.healthy and gate.version is not None
        headers = (
            {}
            if gate.version is None
            else {"x-chalk-weight-version": str(gate.version)}
        )
        return web.json_response(
            {"applied_version": gate.version, "healthy": ready},
            status=200 if ready else 503,
            headers=headers,
        )

    async def prometheus(request: web.Request) -> web.Response:
        return web.Response(text=metrics.prometheus(), content_type="text/plain")

    async def proxy(request: web.Request) -> web.StreamResponse:
        if request.path not in _OPENAI_ROUTES:
            raise web.HTTPNotFound()
        response: web.StreamResponse | None = None
        try:
            async with gate.admit() as version:
                async with app[client_key].request(
                    request.method,
                    engine_url.rstrip("/") + str(request.rel_url),
                    headers=_headers(request.headers),
                    data=request.content.iter_any(),
                    allow_redirects=False,
                ) as upstream:
                    headers = _headers(upstream.headers, response=True)
                    headers["x-chalk-policy-version"] = str(version)
                    response = web.StreamResponse(
                        status=upstream.status, headers=headers
                    )
                    await response.prepare(request)
                    async for chunk in upstream.content.iter_any():
                        await response.write(chunk)
                    await response.write_eof()
                    return response
        except UnhealthyError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from exc
        except (asyncio.CancelledError, ClientError, OSError, TimeoutError) as exc:
            # A disconnected client does not prove the native engine has stopped
            # decoding. Fail closed until the process group is restarted.
            await gate.fail()
            if isinstance(exc, asyncio.CancelledError):
                raise
            if response is not None and response.prepared:
                response.force_close()
                raise
            raise web.HTTPBadGateway(text="Inference engine unavailable") from exc

    app.router.add_get("/chalk/health", health)
    app.router.add_get("/chalk/metrics", prometheus)
    app.router.add_route("*", "/{path:.*}", proxy)
    return app
