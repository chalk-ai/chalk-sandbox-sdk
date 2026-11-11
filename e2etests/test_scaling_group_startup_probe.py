"""Exercise externally routed scaling-group startup probes in a live environment."""

from __future__ import annotations

import pytest
from chalkcompute import ComputeClass, Image, ReadinessProbe, ScalingGroup, StartupProbe
from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import needs_staging

HTTP_SERVER = """
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    checks = 0

    def do_GET(self):
        if self.path == "/health":
            Handler.checks += 1
            status = 503 if Handler.checks <= 2 else 200
        elif self.path == "/checks":
            status = 200
        else:
            status = 404
        self.send_response(status)
        self.end_headers()
        self.wfile.write(str(Handler.checks).encode())

    def log_message(self, *_):
        pass

ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
"""

SLOW_HEALTH_SERVER = """
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            time.sleep(1.5)
        body = b"healthy"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass

ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
"""

GRPC_SERVER = """
from concurrent.futures import ThreadPoolExecutor
import grpc

server = grpc.server(ThreadPoolExecutor(max_workers=4))
check = grpc.unary_unary_rpc_method_handler(
    lambda request, context: b"",
    request_deserializer=lambda data: data,
    response_serializer=lambda data: data,
)
server.add_generic_rpc_handlers((
    grpc.method_handlers_generic_handler("startup.test.Probe", {"Check": check}),
))
assert server.add_insecure_port("0.0.0.0:8080") == 8080
server.start()
server.wait_for_termination()
"""


@needs_staging
def test_http_startup_probe_blocks_availability_until_health_succeeds() -> None:
    group = ScalingGroup(
        name=build_scoped_name("ex-http-startup-probe"),
        image="python:3.12-slim",
        entrypoint=["python", "-u", "-c", HTTP_SERVER],
        port=8080,
        startup_probe=StartupProbe.http("/health"),
    )
    try:
        group.deploy(ready_timeout=180)
        response = group.call("/checks", method="GET")
        assert response.status == 200
        assert int(response.content) >= 3
    finally:
        if group.id is not None:
            group.delete()


@needs_staging
@pytest.mark.parametrize(
    ("method", "name"),
    [
        ("/startup.test.Probe/Check", "ex-grpc-startup-probe-explicit"),
        (None, "ex-grpc-startup-probe-default"),
    ],
)
def test_grpc_startup_probe_reaches_routed_service(
    method: str | None, name: str
) -> None:
    group = ScalingGroup(
        name=build_scoped_name(name),
        image=Image.debian_slim("3.12")
        .pip_install(["grpcio>=1.60,<2"])
        .entrypoint(["python", "-u", "-c", GRPC_SERVER]),
        protocol="grpc",
        port=8080,
        startup_probe=StartupProbe.grpc(method),
    )
    try:
        group.deploy(ready_timeout=180)
        assert group.info is not None
        assert group.info.available_replicas >= 1
    finally:
        if group.id is not None:
            group.delete()


@needs_staging
@pytest.mark.parametrize("compute_class", [ComputeClass.K8S, ComputeClass.HOST])
def test_http_route_check_preserves_longer_readiness_timeout(
    compute_class: ComputeClass,
) -> None:
    """A valid health check taking over one second must still deploy."""
    group = ScalingGroup(
        name=build_scoped_name(f"ex-http-slow-health-{compute_class.name.lower()}"),
        image="python:3.12-slim",
        entrypoint=["python", "-u", "-c", SLOW_HEALTH_SERVER],
        compute_class=compute_class,
        authenticated=True,
        readiness_probe=ReadinessProbe.http("/health", timeout_seconds=5),
    )
    try:
        group.deploy(ready_timeout=180)
        response = group.call("/health", method="GET", timeout=5, cold_start_timeout=0)
        assert response.status == 200, response.content
        assert response.content == b"healthy"
    finally:
        if group.id is not None:
            group.delete()


@needs_staging
@pytest.mark.parametrize("compute_class", [ComputeClass.K8S, ComputeClass.HOST])
def test_http_startup_probe_on_any_path_serves_first_call_after_deploy(
    compute_class: ComputeClass,
) -> None:
    """A startup probe on any served path makes the first call after deploy() succeed."""
    group = ScalingGroup(
        name=build_scoped_name(f"ex-http-route-{compute_class.name.lower()}"),
        image="python:3.12-slim",
        entrypoint=["python", "-u", "-c", HTTP_SERVER],
        compute_class=compute_class,
        authenticated=True,
        startup_probe=StartupProbe.http("/checks"),
    )
    try:
        group.deploy(ready_timeout=180)
        response = group.call("/checks", method="GET", cold_start_timeout=0)
        assert response.status == 200, response.content
        # The startup probe on /checks, not /health, is enough for the first
        # call after deploy() to succeed; /health was never called.
        assert response.content == b"0"
    finally:
        if group.id is not None:
            group.delete()
