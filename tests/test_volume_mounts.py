"""Unit tests for volume-mount type preservation (#4) and the pre-send guard (#5).

These exercise the shared helpers in ``chalkcompute._volume_mounts`` directly
(mocking ``GetScalingGroup`` / ``GetContainer`` responses) and the wired
container deploy path end-to-end via a mocked RPC client.
"""

from __future__ import annotations

from typing import Any

import pytest

from chalkcompute._container import Container, ContainerError
from chalkcompute._volume_mounts import (
    fetch_container_volume_types,
    fetch_scaling_group_volume_types,
    guard_volume_mounts,
    preserve_existing_volume_types,
)


class _Resp:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


class _RecordingClient:
    """Returns a queued response per (method) and records every call."""

    def __init__(self, responses: dict[str, _Resp]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any], float]] = []

    def rpc(
        self, service: str, method: str, body: dict[str, Any], timeout: float = 30
    ) -> _Resp:
        self.calls.append((service, method, body, timeout))
        return self.responses[method]


def _sg_resp_with_volumes(volumes: list[dict[str, Any]]) -> _Resp:
    return _Resp(
        {
            "scalingGroup": {
                "name": "sg",
                "status": "Running",
                "spec": {"containerSpec": {"volumes": volumes}},
            }
        }
    )


# ---------------------------------------------------------------------------
# fetch_scaling_group_volume_types (#4 read)
# ---------------------------------------------------------------------------


def test_fetch_scaling_group_volume_types_extracts_name_to_type() -> None:
    client = _RecordingClient(
        {
            "GetScalingGroup": _sg_resp_with_volumes(
                [
                    {"name": "vol-a", "mountPath": "/a", "type": "chalkfs"},
                    {"name": "vol-b", "mountPath": "/b", "type": "versioned_chalkfs"},
                ]
            )
        }
    )
    types = fetch_scaling_group_volume_types(client, "svc", "sg")
    assert types == {"vol-a": "chalkfs", "vol-b": "versioned_chalkfs"}


def test_fetch_scaling_group_volume_types_not_found_returns_empty() -> None:
    client = _RecordingClient(
        {"GetScalingGroup": _Resp({"message": "not found"}, status_code=404)}
    )
    # A not-found / first-deploy scaling group must not error the deploy.
    assert fetch_scaling_group_volume_types(client, "svc", "sg") == {}


def test_fetch_scaling_group_volume_types_missing_nesting_returns_empty() -> None:
    client = _RecordingClient({"GetScalingGroup": _Resp({"scalingGroup": {}})})
    assert fetch_scaling_group_volume_types(client, "svc", "sg") == {}


def test_fetch_scaling_group_volume_types_rpc_raises_returns_empty() -> None:
    class _Boom:
        def rpc(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("transport down")

    assert fetch_scaling_group_volume_types(_Boom(), "svc", "sg") == {}


def test_fetch_container_volume_types_reads_container_spec_volumes() -> None:
    client = _RecordingClient(
        {
            "GetContainer": _Resp(
                {
                    "container": {
                        "spec": {
                            "volumes": [
                                {"name": "v", "mountPath": "/v", "type": "chalkfs"}
                            ]
                        }
                    }
                }
            )
        }
    )
    assert fetch_container_volume_types(client, "svc", "c") == {"v": "chalkfs"}


# ---------------------------------------------------------------------------
# preserve_existing_volume_types (#4 preserve)
# ---------------------------------------------------------------------------


def test_preserve_reuses_server_type_for_existing_volume() -> None:
    mounts = [
        {"name": "vol-a", "mountPath": "/a", "type": "versioned_chalkfs"},
        {"name": "vol-new", "mountPath": "/n", "type": "versioned_chalkfs"},
    ]
    # Server says vol-a is the non-versioned chalkfs; outgoing must adopt it.
    preserve_existing_volume_types(mounts, {"vol-a": "chalkfs"})
    assert mounts[0]["type"] == "chalkfs"
    # New volume is left untouched.
    assert mounts[1]["type"] == "versioned_chalkfs"


def test_preserve_noop_on_first_deploy() -> None:
    mounts = [{"name": "vol-a", "mountPath": "/a", "type": "versioned_chalkfs"}]
    preserve_existing_volume_types(mounts, {})
    assert mounts[0]["type"] == "versioned_chalkfs"


# ---------------------------------------------------------------------------
# guard_volume_mounts (#5)
# ---------------------------------------------------------------------------


def test_guard_rejects_two_types_for_same_name_in_request() -> None:
    mounts = [
        {"name": "vol-a", "mountPath": "/a1", "type": "chalkfs"},
        {"name": "vol-a", "mountPath": "/a2", "type": "versioned_chalkfs"},
    ]
    with pytest.raises(ContainerError, match="Conflicting mount types for volume"):
        guard_volume_mounts(
            mounts, existing_types={}, error_cls=ContainerError, owner="x"
        )


def test_guard_rejects_changing_existing_volume_type() -> None:
    mounts = [{"name": "vol-a", "mountPath": "/a", "type": "versioned_chalkfs"}]
    with pytest.raises(ContainerError, match="Cannot change mount type"):
        guard_volume_mounts(
            mounts,
            existing_types={"vol-a": "chalkfs"},
            error_cls=ContainerError,
            owner="x",
        )


def test_guard_allows_consistent_mounts() -> None:
    mounts = [
        {"name": "vol-a", "mountPath": "/a", "type": "chalkfs"},
        {"name": "vol-b", "mountPath": "/b", "type": "versioned_chalkfs"},
    ]
    # Same name twice with the SAME type is fine.
    mounts.append({"name": "vol-a", "mountPath": "/a2", "type": "chalkfs"})
    guard_volume_mounts(
        mounts,
        existing_types={"vol-a": "chalkfs"},
        error_cls=ContainerError,
        owner="x",
    )


def test_guard_error_message_names_volume_and_types() -> None:
    mounts = [{"name": "data", "mountPath": "/d", "type": "versioned_chalkfs"}]
    with pytest.raises(ContainerError) as exc:
        guard_volume_mounts(
            mounts,
            existing_types={"data": "chalkfs"},
            error_cls=ContainerError,
            owner="container 'c'",
        )
    msg = str(exc.value)
    assert "data" in msg and "chalkfs" in msg and "versioned_chalkfs" in msg


# ---------------------------------------------------------------------------
# End-to-end wiring through Container.run (#4 + #5)
# ---------------------------------------------------------------------------


def _run_resp() -> _Resp:
    return _Resp(
        {
            "container": {
                "id": "c-1",
                "name": "c",
                "status": "Running",
                "podName": "pod",
            }
        }
    )


def test_container_run_preserves_existing_volume_type() -> None:
    # Container would default to versioned_chalkfs, but the server already
    # records this volume as plain chalkfs — the outgoing request must adopt it.
    responses = {
        "GetContainer": _Resp(
            {
                "container": {
                    "spec": {
                        "volumes": [
                            {"name": "my-volume", "mountPath": "/data", "type": "chalkfs"}
                        ]
                    }
                }
            }
        ),
        "RunContainer": _run_resp(),
    }
    client = _RecordingClient(responses)
    container = Container(image="ubuntu:latest", name="c").mount_volume(
        "my-volume", "/data"
    )
    container._client = client

    container.run()

    run_call = next(c for c in client.calls if c[1] == "RunContainer")
    volumes = run_call[2]["container"]["spec"]["volumes"]
    assert volumes == [
        {"name": "my-volume", "mountPath": "/data", "type": "chalkfs"}
    ]


def test_container_run_first_deploy_keeps_default_type() -> None:
    # GetContainer 404 (first deploy) -> nothing to preserve; default kept.
    responses = {
        "GetContainer": _Resp({"message": "not found"}, status_code=404),
        "RunContainer": _run_resp(),
    }
    client = _RecordingClient(responses)
    container = Container(image="ubuntu:latest", name="c").mount_volume(
        "my-volume", "/data"
    )
    container._client = client

    container.run()

    run_call = next(c for c in client.calls if c[1] == "RunContainer")
    volumes = run_call[2]["container"]["spec"]["volumes"]
    assert volumes == [
        {"name": "my-volume", "mountPath": "/data", "type": "versioned_chalkfs"}
    ]


def test_container_run_probes_existing_state_before_running() -> None:
    # Verify the deploy path consults GetContainer (the #4 read) before issuing
    # RunContainer whenever volumes are present, so preservation/guarding apply.
    responses = {
        "GetContainer": _Resp(
            {
                "container": {
                    "spec": {
                        "volumes": [
                            {"name": "my-volume", "mountPath": "/data", "type": "chalkfs"}
                        ]
                    }
                }
            }
        ),
        "RunContainer": _run_resp(),
    }
    client = _RecordingClient(responses)
    container = Container(image="ubuntu:latest", name="c").mount_volume(
        "my-volume", "/data"
    )
    container._client = client

    container.run()

    methods = [c[1] for c in client.calls]
    assert methods.index("GetContainer") < methods.index("RunContainer")


def test_container_run_guard_blocks_send_on_conflict(monkeypatch: Any) -> None:
    # If the outgoing request somehow carries the same volume name with two
    # different types (e.g. two reconstructed request objects), the guard must
    # fail fast and never reach RunContainer. We simulate that by having the
    # preservation step return divergent server state per call is not possible
    # in one request, so we directly assert the guard wired into run() by
    # making preservation a no-op and supplying conflicting outgoing mounts.
    import chalkcompute._container as container_mod

    responses = {
        "GetContainer": _Resp({"message": "not found"}, status_code=404),
        "RunContainer": _run_resp(),
    }
    client = _RecordingClient(responses)
    container = (
        Container(image="ubuntu:latest", name="c")
        .mount_volume("dup", "/a")
        .mount_volume("dup", "/b")
    )
    container._client = client

    # Force the two same-named mounts to diverge in type after they are built,
    # exercising guard rule (a) inside the real run() path.
    real_preserve = container_mod.preserve_existing_volume_types

    def _diverge(mounts: list[dict[str, Any]], existing: Any) -> None:
        real_preserve(mounts, existing)
        if len(mounts) == 2:
            mounts[1]["type"] = "chalkfs"

    monkeypatch.setattr(container_mod, "preserve_existing_volume_types", _diverge)

    with pytest.raises(ContainerError, match="Conflicting mount types"):
        container.run()

    assert not any(c[1] == "RunContainer" for c in client.calls)


# ---------------------------------------------------------------------------
# End-to-end wiring through RemoteFunction.deploy (#4 + #5)
# ---------------------------------------------------------------------------


class _FakeDriver:
    """Stand-in VolumeDriver that reports a fixed mount type."""

    def __init__(self, mount_type: str) -> None:
        self.mount_type = mount_type


def _patch_function_build(monkeypatch: Any, *, mount_type: str) -> None:
    """Replace the image build / schema steps so deploy() reaches the RPC fast."""
    import chalkcompute._function as fn_mod
    from chalkcompute._gen.chalk.arrow.v1 import arrow_pb2

    monkeypatch.setattr(
        fn_mod.RemoteFunction, "_build_deploy_image", lambda self: object()
    )
    monkeypatch.setattr(
        fn_mod,
        "_build_and_upload",
        lambda *a, **k: ("img:latest", [], _FakeDriver(mount_type)),
    )
    monkeypatch.setattr(
        fn_mod, "_build_schemas", lambda func: (arrow_pb2.Schema(), arrow_pb2.Schema())
    )


def _efv_resp() -> _Resp:
    return _Resp(
        {
            "externalFunctionVersion": {
                "id": "efv-1",
                "functionName": "embed",
                "version": 1,
                "scalingGroupName": "embed",
            },
            "scalingGroup": {"status": "Running", "port": 6666},
        }
    )


def test_function_deploy_preserves_existing_volume_type(monkeypatch: Any) -> None:
    from chalkcompute import RemoteFunction

    _patch_function_build(monkeypatch, mount_type="versioned_chalkfs")

    def embed(text: str) -> str:
        return text

    fn = RemoteFunction(embed, name="embed", volumes=[("vol-a", "/data")])
    # Server already records vol-a as plain chalkfs.
    client = _RecordingClient(
        {
            "GetScalingGroup": _sg_resp_with_volumes(
                [{"name": "vol-a", "mountPath": "/data", "type": "chalkfs"}]
            ),
            "CreateExternalFunctionVersion": _efv_resp(),
        }
    )
    fn._client = client

    fn.deploy()

    create_call = next(
        c for c in client.calls if c[1] == "CreateExternalFunctionVersion"
    )
    volumes = create_call[2]["spec"]["containerSpec"]["volumes"]
    assert volumes == [{"name": "vol-a", "mountPath": "/data", "type": "chalkfs"}]


def test_function_deploy_guard_blocks_type_change(monkeypatch: Any) -> None:
    from chalkcompute import RemoteFunction
    from chalkcompute._function import FunctionError
    import chalkcompute._function as fn_mod

    _patch_function_build(monkeypatch, mount_type="versioned_chalkfs")

    # Neutralize preservation so the guard sees a genuine type change vs server.
    monkeypatch.setattr(
        fn_mod, "preserve_existing_volume_types", lambda mounts, existing: None
    )

    def embed(text: str) -> str:
        return text

    fn = RemoteFunction(embed, name="embed", volumes=[("vol-a", "/data")])
    client = _RecordingClient(
        {
            "GetScalingGroup": _sg_resp_with_volumes(
                [{"name": "vol-a", "mountPath": "/data", "type": "chalkfs"}]
            ),
            "CreateExternalFunctionVersion": _efv_resp(),
        }
    )
    fn._client = client

    with pytest.raises(FunctionError, match="Cannot change mount type"):
        fn.deploy()

    assert not any(
        c[1] == "CreateExternalFunctionVersion" for c in client.calls
    )


def test_function_deploy_first_deploy_keeps_default(monkeypatch: Any) -> None:
    from chalkcompute import RemoteFunction

    _patch_function_build(monkeypatch, mount_type="versioned_chalkfs")

    def embed(text: str) -> str:
        return text

    fn = RemoteFunction(embed, name="embed", volumes=[("vol-a", "/data")])
    client = _RecordingClient(
        {
            "GetScalingGroup": _Resp({"message": "not found"}, status_code=404),
            "CreateExternalFunctionVersion": _efv_resp(),
        }
    )
    fn._client = client

    fn.deploy()

    create_call = next(
        c for c in client.calls if c[1] == "CreateExternalFunctionVersion"
    )
    volumes = create_call[2]["spec"]["containerSpec"]["volumes"]
    assert volumes == [
        {"name": "vol-a", "mountPath": "/data", "type": "versioned_chalkfs"}
    ]
