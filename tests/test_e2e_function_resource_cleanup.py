"""The e2e suite only removes source volumes attached to its own Functions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from chalkcompute import VolumeError
from e2etests import conftest as e2e


def test_source_cleanup_uses_the_deployed_revision_and_rejects_shared_volumes() -> None:
    function_name = "test-fn-secret-abcd1234"
    client = Mock()
    client.stub.return_value = SimpleNamespace(
        get_external_function_version_source=object()
    )
    function = SimpleNamespace(
        version_info=SimpleNamespace(id="version-1"), _client=client
    )
    client.call.return_value = SimpleNamespace(
        volume_name=f"fn-src-{function_name}-app"
    )

    assert e2e._test_owned_source_volume_name(function, function_name) == (
        f"fn-src-{function_name}-app"
    )
    assert client.call.call_args.args[1].function_version_id == "version-1"

    client.call.return_value = SimpleNamespace(volume_name=function_name)
    assert e2e._test_owned_source_volume_name(function, function_name) == function_name

    client.call.return_value = SimpleNamespace(volume_name="chalk-fn-src-shared")
    assert e2e._test_owned_source_volume_name(function, function_name) is None


def test_source_cleanup_waits_for_the_function_mount_to_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volumes = MagicMock()
    volumes.__enter__.return_value = volumes
    volumes.exists.return_value = True
    volumes.delete_volume.side_effect = [VolumeError("still mounted"), None]
    monkeypatch.setattr(e2e.VolumeClient, "from_connect", Mock(return_value=volumes))
    monkeypatch.setattr(e2e.time, "sleep", Mock())

    function = SimpleNamespace(_client=object())
    e2e._delete_test_owned_source_volume(function, "fn-src-test-fn-secret-abcd1234-app")

    assert volumes.delete_volume.call_count == 2
    e2e.time.sleep.assert_called_once_with(2)
