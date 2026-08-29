import importlib
import logging
import types
from pathlib import Path
from unittest.mock import patch

import pytest

import chalkcompute._remote_call_client as remote_call_client
from chalkcompute._remote_call_client import (
    RemoteCallClient,
    RemoteCallClientUnavailable,
    RemoteCallTransportError,
    RemoteCallUnavailableError,
)


class LegacyNativeRemoteCallClient:
    calls: list[tuple[object, ...]] = []
    method_calls: list[tuple[object, ...]] = []
    call_ipc_errors: list[BaseException] = []

    def __init__(self, *args: object) -> None:
        self.closed = False
        self.calls.append(args)
        if len(args) > 7:
            raise TypeError(
                "RemoteCallClient.__new__() takes from 1 to 7 positional "
                "arguments but 12 were given"
            )

    def call_ipc(self, name: str, input_ipc: bytes) -> list[bytes]:
        self.method_calls.append(("call_ipc", name, input_ipc))
        if self.call_ipc_errors:
            raise self.call_ipc_errors.pop(0)
        return [b"ok"]

    def enqueue(self, name: str, input_ipc: bytes) -> str:
        self.method_calls.append(("enqueue", name, input_ipc))
        return "call-1"

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def legacy_native_client() -> None:
    LegacyNativeRemoteCallClient.calls = []
    LegacyNativeRemoteCallClient.method_calls = []
    LegacyNativeRemoteCallClient.call_ipc_errors = []
    with (
        patch.object(
            remote_call_client,
            "_warned_per_call_metadata_unsupported",
            False,
        ),
        patch.object(
            remote_call_client,
            "_NativeRemoteCallClient",
            LegacyNativeRemoteCallClient,
        ),
    ):
        yield


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


def test_remote_call_errors_remain_runtime_errors() -> None:
    assert issubclass(RemoteCallUnavailableError, RuntimeError)
    assert issubclass(RemoteCallTransportError, RuntimeError)


def test_call_ipc_retries_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(remote_call_client.time, "sleep", lambda _seconds: None)
    LegacyNativeRemoteCallClient.call_ipc_errors = [
        RemoteCallUnavailableError("not ready")
    ]
    client = RemoteCallClient("remote.chalk.test:443")

    assert client.call_ipc("handler", b"in") == [b"ok"]
    assert LegacyNativeRemoteCallClient.method_calls == [
        ("call_ipc", "handler", b"in"),
        ("call_ipc", "handler", b"in"),
    ]


@pytest.mark.parametrize(
    "message",
    [
        "grpc-status header missing, mapped from HTTP status code 504",
        "upstream request timeout",
        "Gateway Timeout",
        "Your scale-to-zero workload failed to scale in time.",
    ],
)
def test_call_ipc_does_not_retry_gateway_timeout(message: str) -> None:
    LegacyNativeRemoteCallClient.call_ipc_errors = [RemoteCallUnavailableError(message)]
    client = RemoteCallClient("remote.chalk.test:443")

    with pytest.raises(RemoteCallUnavailableError, match=message):
        client.call_ipc("handler", b"in")

    assert LegacyNativeRemoteCallClient.method_calls == [("call_ipc", "handler", b"in")]


def test_call_ipc_stops_retrying_at_deadline(monkeypatch) -> None:
    monkeypatch.setattr(remote_call_client, "_DIRECT_CALL_RETRY_TIMEOUT_SECONDS", 0)
    LegacyNativeRemoteCallClient.call_ipc_errors = [
        RemoteCallUnavailableError("not ready")
    ]
    client = RemoteCallClient("remote.chalk.test:443")

    with pytest.raises(RemoteCallUnavailableError, match="not ready"):
        client.call_ipc("handler", b"in")

    assert LegacyNativeRemoteCallClient.method_calls == [("call_ipc", "handler", b"in")]


def test_legacy_native_drops_per_call_metadata_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = RemoteCallClient("remote.chalk.test:443")

    caplog.set_level(logging.WARNING, logger="chalkcompute._remote_call_client")

    assert client.call_ipc(
        "handler", b"in", metadata=(("traceparent", "trace-1"),)
    ) == [b"ok"]
    assert client.call_ipc(
        "handler", b"in", metadata=(("traceparent", "trace-2"),)
    ) == [b"ok"]
    assert (
        client.enqueue("pkg.fn", b"in", metadata=(("traceparent", "trace"),))
        == "call-1"
    )
    assert LegacyNativeRemoteCallClient.method_calls == [
        ("call_ipc", "handler", b"in"),
        ("call_ipc", "handler", b"in"),
        ("enqueue", "pkg.fn", b"in"),
    ]
    assert caplog.text.count("does not support per-call metadata") == 1


def test_close_releases_native_client() -> None:
    client = RemoteCallClient("remote.chalk.test:443")
    inner = client._inner

    client.close()
    client.close()

    assert inner.closed is True
    assert client._closed is True
    assert client._inner is None


def test_find_native_extension_path_skips_local_overlay(
    tmp_path: Path,
) -> None:
    overlay_root = tmp_path / "overlay"
    site_packages_root = tmp_path / "site-packages"
    overlay_package = overlay_root / "chalkcompute"
    site_packages_package = site_packages_root / "chalkcompute"
    overlay_package.mkdir(parents=True)
    site_packages_package.mkdir(parents=True)
    native_path = site_packages_package / "_native.cpython-311-x86_64-linux-gnu.so"
    native_path.write_bytes(b"")

    with (
        patch.object(
            remote_call_client,
            "_PACKAGE_DIR",
            overlay_package.resolve(),
        ),
        patch.object(
            remote_call_client.importlib.machinery,
            "EXTENSION_SUFFIXES",
            [".cpython-311-x86_64-linux-gnu.so"],
        ),
    ):
        assert (
            remote_call_client._find_native_extension_path(
                [str(overlay_root), str(site_packages_root)]
            )
            == native_path
        )


def test_import_native_falls_back_to_site_packages(
    tmp_path: Path,
) -> None:
    overlay_root = tmp_path / "overlay"
    site_packages_root = tmp_path / "site-packages"
    overlay_package = overlay_root / "chalkcompute"
    site_packages_package = site_packages_root / "chalkcompute"
    overlay_package.mkdir(parents=True)
    site_packages_package.mkdir(parents=True)
    native_path = site_packages_package / "_native.cpython-311-x86_64-linux-gnu.so"
    native_path.write_bytes(b"not-a-real-extension")

    def fake_import_module(name: str) -> types.ModuleType:
        raise ImportError(f"missing {name}")

    class FakeLoader:
        def create_module(self, spec: importlib.machinery.ModuleSpec) -> None:
            return None

        def exec_module(self, module: types.ModuleType) -> None:
            module.RemoteCallClient = LegacyNativeRemoteCallClient
            module.resolve_service_auth_context = lambda: None
            module.__file__ = str(native_path)

    def fake_spec_from_file_location(name: str, location: Path):
        assert name == "chalkcompute._native"
        assert Path(location) == native_path
        return importlib.machinery.ModuleSpec(name=name, loader=FakeLoader())

    with (
        patch.object(
            remote_call_client,
            "_PACKAGE_DIR",
            overlay_package.resolve(),
        ),
        patch.object(
            remote_call_client.importlib.machinery,
            "EXTENSION_SUFFIXES",
            [".cpython-311-x86_64-linux-gnu.so"],
        ),
        patch.object(
            remote_call_client.sys,
            "path",
            [str(overlay_root), str(site_packages_root)],
        ),
        patch.object(
            remote_call_client.importlib,
            "import_module",
            fake_import_module,
        ),
        patch.object(
            remote_call_client.importlib.util,
            "spec_from_file_location",
            fake_spec_from_file_location,
        ),
    ):
        module = remote_call_client._import_native()

    assert module.RemoteCallClient is LegacyNativeRemoteCallClient
    assert module.__file__ == str(native_path)
