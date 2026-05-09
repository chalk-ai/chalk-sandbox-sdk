import logging

import pytest

import chalkcompute._remote_call_client as remote_call_client
from chalkcompute._remote_call_client import RemoteCallClient, RemoteCallClientUnavailable


class LegacyNativeRemoteCallClient:
    calls: list[tuple[object, ...]] = []
    method_calls: list[tuple[object, ...]] = []

    def __init__(self, *args: object) -> None:
        self.calls.append(args)
        if len(args) > 7:
            raise TypeError(
                "RemoteCallClient.__new__() takes from 1 to 7 positional "
                "arguments but 12 were given"
            )

    def call_ipc(self, name: str, input_ipc: bytes) -> list[bytes]:
        self.method_calls.append(("call_ipc", name, input_ipc))
        return [b"ok"]

    def enqueue(self, name: str, input_ipc: bytes) -> str:
        self.method_calls.append(("enqueue", name, input_ipc))
        return "call-1"


@pytest.fixture(autouse=True)
def legacy_native_client(monkeypatch: pytest.MonkeyPatch) -> None:
    LegacyNativeRemoteCallClient.calls = []
    LegacyNativeRemoteCallClient.method_calls = []
    monkeypatch.setattr(
        remote_call_client,
        "_warned_per_call_metadata_unsupported",
        False,
    )
    monkeypatch.setattr(
        remote_call_client,
        "_NativeRemoteCallClient",
        LegacyNativeRemoteCallClient,
    )


def test_legacy_native_constructor_fallback_without_service_auth() -> None:
    client = RemoteCallClient(
        "remote.chalk.test:443",
        metadata=(("x-chalk-env-id", "env-id"),),
        use_tls=True,
    )

    assert isinstance(client._inner, LegacyNativeRemoteCallClient)
    assert len(LegacyNativeRemoteCallClient.calls) == 2
    assert len(LegacyNativeRemoteCallClient.calls[0]) == 12
    assert LegacyNativeRemoteCallClient.calls[1] == (
        "remote.chalk.test:443",
        [("x-chalk-env-id", "env-id")],
        True,
        50,
        5000,
        1.7,
        None,
    )


def test_legacy_native_constructor_rejected_for_service_auth() -> None:
    with pytest.raises(RemoteCallClientUnavailable, match="too old for service-auth"):
        RemoteCallClient(
            "remote.chalk.test:443",
            auth_endpoint="https://api.chalk.test",
            exchange_token="exchange-token",
            requested_permissions=(2, 3, 11),
        )


def test_legacy_native_call_shape_still_works_without_per_call_metadata() -> None:
    client = RemoteCallClient("remote.chalk.test:443")

    assert client.call_ipc("handler", b"in") == [b"ok"]
    assert client.enqueue("pkg.fn", b"in") == "call-1"
    assert LegacyNativeRemoteCallClient.method_calls == [
        ("call_ipc", "handler", b"in"),
        ("enqueue", "pkg.fn", b"in"),
    ]


def test_legacy_native_drops_per_call_metadata_once(caplog: pytest.LogCaptureFixture) -> None:
    client = RemoteCallClient("remote.chalk.test:443")

    caplog.set_level(logging.WARNING, logger="chalkcompute._remote_call_client")

    assert client.call_ipc("handler", b"in", metadata=(("traceparent", "trace-1"),)) == [b"ok"]
    assert client.call_ipc("handler", b"in", metadata=(("traceparent", "trace-2"),)) == [b"ok"]
    assert client.enqueue("pkg.fn", b"in", metadata=(("traceparent", "trace"),)) == "call-1"
    assert LegacyNativeRemoteCallClient.method_calls == [
        ("call_ipc", "handler", b"in"),
        ("call_ipc", "handler", b"in"),
        ("enqueue", "pkg.fn", b"in"),
    ]
    assert caplog.text.count("tracing metadata was dropped") == 1
