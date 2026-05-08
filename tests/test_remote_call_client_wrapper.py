import pytest

import chalkcompute._remote_call_client as remote_call_client
from chalkcompute._remote_call_client import RemoteCallClient, RemoteCallClientUnavailable


class LegacyNativeRemoteCallClient:
    calls: list[tuple[object, ...]] = []

    def __init__(self, *args: object) -> None:
        self.calls.append(args)
        if len(args) > 7:
            raise TypeError(
                "RemoteCallClient.__new__() takes from 1 to 7 positional "
                "arguments but 12 were given"
            )


@pytest.fixture(autouse=True)
def legacy_native_client(monkeypatch: pytest.MonkeyPatch) -> None:
    LegacyNativeRemoteCallClient.calls = []
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
