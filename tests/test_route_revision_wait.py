"""Deploys wait until a function's stable route answers from the new revision."""

from __future__ import annotations

import importlib.metadata
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from protobuf import Oneof

import chalkcompute._function as function_mod
from chalkcompute._function import FunctionError, RemoteFunction
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as catalog_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._image import Image
from chalkcompute._scaling_group import ReadinessProbe
from tests._fakes import FakeMethod, StubbedConnectClient

NEW = "rev-new"
OLD = "rev-old"


def _add(x: int) -> int:
    return x


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _function_with_probes(
    monkeypatch: pytest.MonkeyPatch, observations: list[str | None | Exception]
) -> tuple[RemoteFunction, list[str | None | Exception], _FakeClock]:
    clock = _FakeClock()
    monkeypatch.setattr(function_mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(function_mod.time, "sleep", clock.sleep)
    remaining = list(observations)
    fn = RemoteFunction(_add, name="fn-route")

    def probe() -> str | None:
        clock.now += 1.0
        observed = remaining.pop(0) if remaining else observations[-1]
        if isinstance(observed, Exception):
            raise observed
        return observed

    monkeypatch.setattr(fn, "_probe_route_revision", probe)
    return fn, remaining, clock


def test_wait_requires_consecutive_confirmations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fn, remaining, _ = _function_with_probes(
        monkeypatch, [OLD, NEW, OLD, NEW, NEW, NEW, OLD]
    )

    fn._wait_for_route_revision(revision_id=NEW, poll_interval=1.0, deadline=1_000.0)

    assert remaining == [OLD]


def test_wait_retries_through_cutover_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    unavailable = ConnectionError("upstream connect error")
    fn, remaining, _ = _function_with_probes(
        monkeypatch, [OLD, unavailable, unavailable, NEW, NEW, NEW]
    )

    fn._wait_for_route_revision(revision_id=NEW, poll_interval=1.0, deadline=1_000.0)

    assert remaining == []


def test_wait_fails_while_previous_revision_keeps_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fn, _, clock = _function_with_probes(monkeypatch, [OLD])

    with pytest.raises(
        FunctionError, match="previous version keeps serving"
    ) as excinfo:
        fn._wait_for_route_revision(revision_id=NEW, poll_interval=1.0, deadline=60.0)

    assert OLD in str(excinfo.value)
    assert clock.now >= 60.0


def test_wait_gives_up_on_routes_that_report_no_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fn, _, clock = _function_with_probes(monkeypatch, [None])

    fn._wait_for_route_revision(revision_id=NEW, poll_interval=1.0, deadline=1_000.0)

    assert function_mod._ROUTE_REVISION_UNREPORTED_GRACE_SECONDS <= clock.now < 1_000.0


class _EnsureClient(StubbedConnectClient):
    def __init__(self, scaling_group: sg_pb.ScalingGroupResponse) -> None:
        self.scaling_group = scaling_group
        self.requests: dict[str, Any] = {}
        self._env_id: str | None = None

    def _ensure_auth(self) -> None:
        pass

    def call(self, method: FakeMethod, request: Any, **_: Any) -> Any:
        self.requests[method.name] = request
        if method.name != "ensure_external_function":
            raise AssertionError(f"unexpected method: {method.name}")
        container_spec = request.spec.version_spec.spec.container_spec
        return catalog_pb.EnsureExternalFunctionResponse(
            result=Oneof(
                "complete",
                catalog_pb.EnsuredExternalFunction(
                    external_function=catalog_pb.ExternalFunction(
                        id="fn-version-2",
                        name=request.function_name,
                        current_version=catalog_pb.ExternalFunctionVersion(
                            id="fn-version-2",
                            function_id="fn-1",
                            function_name=request.function_name,
                            version=2,
                            scaling_group_name=container_spec.name,
                        ),
                    ),
                    scaling_group=self.scaling_group,
                ),
            )
        )


def _deploy(
    monkeypatch: pytest.MonkeyPatch, fn: RemoteFunction, revision_id: str
) -> tuple[_EnsureClient, list[str]]:
    client = _EnsureClient(
        sg_pb.ScalingGroupResponse(
            name="fn-route",
            revision_id=revision_id,
            status="Available",
            ready_replicas=1,
            available_replicas=1,
            web_url="https://fn-route.example",
        )
    )
    fn._client = client  # type: ignore[assignment]
    monkeypatch.setattr(
        RemoteFunction, "_build_deploy_image", lambda _: Image("image:test")
    )
    awaited: list[str] = []
    monkeypatch.setattr(
        fn,
        "_wait_for_route_revision",
        lambda *, revision_id, poll_interval, deadline: awaited.append(revision_id),
    )
    fn.deploy(poll_interval=0, ready_timeout=10)
    return client, awaited


def test_deploy_waits_for_route_to_serve_new_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, awaited = _deploy(monkeypatch, RemoteFunction(_add, name="fn-route"), NEW)
    assert awaited == [NEW]


def test_deploy_does_not_wake_scale_to_zero_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fn = RemoteFunction(_add, name="fn-route", min_replicas=0)
    _, awaited = _deploy(monkeypatch, fn, NEW)
    assert awaited == []


def test_deploy_skips_route_wait_without_a_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, awaited = _deploy(monkeypatch, RemoteFunction(_add, name="fn-route"), "")
    assert awaited == []


def _readiness_probe(client: _EnsureClient) -> container_pb.ReadinessProbe:
    request = client.requests["ensure_external_function"]
    probe = request.spec.version_spec.spec.container_spec.readiness_probe
    assert probe is not None
    return probe


def test_function_replicas_default_to_grpc_readiness_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _deploy(monkeypatch, RemoteFunction(_add, name="fn-route"), NEW)
    probe = _readiness_probe(client)
    assert probe.check is not None and probe.check.field == "grpc"
    assert probe.period_seconds == 1


def test_function_readiness_probe_override_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fn = RemoteFunction(
        _add, name="fn-route", readiness_probe=ReadinessProbe.grpc(period_seconds=7)
    )
    client, _ = _deploy(monkeypatch, fn, NEW)
    assert _readiness_probe(client).period_seconds == 7


def _runtime_reports_revision() -> bool:
    version = importlib.metadata.version("chalk-remote-call-python")
    return tuple(int(part) for part in version.split(".")[:2]) >= (1, 11)


@pytest.fixture()
def runtime_server_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    from chalk_remote_call.server import serve

    monkeypatch.setenv("CHALK_SCALING_GROUP_REVISION", NEW)
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
        s.bind(("::1", 0))
        port = s.getsockname()[1]
    threading.Thread(
        target=serve,
        kwargs={
            "handler": lambda event, ctx: event,
            "host": "[::1]",
            "port": port,
            "workers": 1,
        },
        daemon=True,
    ).start()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("::1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.05)
    yield f"http://[::1]:{port}"


@pytest.mark.skipif(
    not _runtime_reports_revision(),
    reason="chalk-remote-call-python < 1.11 does not report its revision",
)
def test_probe_reads_revision_from_runtime(runtime_server_url: str) -> None:
    fn = RemoteFunction(_add, name="fn-route")
    fn._web_url = runtime_server_url
    fn._remote_call_metadata = lambda *, queue: []  # type: ignore[method-assign]
    assert fn._probe_route_revision() == NEW


def test_class_deploy_waits_for_each_method_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import chalkcompute._cls as cls_mod
    from chalkcompute._cls import RemoteClass, method

    class Adder:
        @method
        def add(self, x: int) -> int:
            return x

    client = _EnsureClient(
        sg_pb.ScalingGroupResponse(
            name="cls-route",
            revision_id=NEW,
            status="Available",
            ready_replicas=1,
            available_replicas=1,
            web_url="https://cls-route.example",
        )
    )
    rc = RemoteClass(Adder, name="cls-route")
    rc._client = client  # type: ignore[assignment]
    monkeypatch.setattr(
        RemoteClass, "_build_deploy_image", lambda _: (Image("image:test"), [])
    )
    monkeypatch.setattr(cls_mod.time, "sleep", lambda _: None)
    awaited: list[str] = []
    monkeypatch.setattr(
        RemoteFunction,
        "_wait_for_route_revision",
        lambda self, *, revision_id, poll_interval, deadline: awaited.append(
            revision_id
        ),
    )

    rc.deploy(poll_interval=0, ready_timeout=10)

    assert awaited == [NEW]
    assert _readiness_probe(client).period_seconds == 1
