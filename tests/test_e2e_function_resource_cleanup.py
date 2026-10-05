"""The e2e suite only removes source volumes attached to its own Functions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from chalkcompute import VolumeError
from e2etests import conftest as e2e
from e2etests import test_function_secrets, test_remote_function_schedule
from e2etests._ci_resources import (
    SCOPED_NAME_RE,
    build_scoped_name,
    build_test_function_name,
)


def test_cleanup_function_names_are_unique_without_buildkite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BUILDKITE_JOB_ID", raising=False)
    monkeypatch.delenv("BUILDKITE_BUILD_ID", raising=False)
    first = build_test_function_name("ex-count-up")
    second = build_test_function_name("ex-count-up")
    assert first != second
    assert SCOPED_NAME_RE.fullmatch(first)

    monkeypatch.setenv("BUILDKITE_JOB_ID", "job-123")
    assert build_test_function_name("ex-count-up") == build_scoped_name("ex-count-up")


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


def test_source_metadata_failure_still_deletes_the_new_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = SimpleNamespace()
    delete = Mock()
    monkeypatch.setattr(
        e2e.chalkcompute.RemoteFunction, "from_name", Mock(return_value=function)
    )
    monkeypatch.setattr(
        e2e,
        "_test_owned_source_volume_name",
        Mock(side_effect=RuntimeError("source unavailable")),
    )
    monkeypatch.setattr(e2e, "_delete_function", delete)

    with pytest.raises(RuntimeError, match="source unavailable"):
        e2e._deployed_test_function_source("ex-count-up-test")

    delete.assert_called_once_with("ex-count-up-test")


def test_scheduled_deploy_failure_cleans_registered_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = SimpleNamespace()
    delete_function = Mock()
    delete_volume = Mock()
    monkeypatch.setattr(
        e2e, "_deploy_script", Mock(side_effect=pytest.fail.Exception("not ready"))
    )
    monkeypatch.setattr(
        e2e,
        "_deployed_test_function_source",
        Mock(return_value=(function, "ex-heartbeat-test")),
    )
    monkeypatch.setattr(e2e, "_delete_function", delete_function)
    monkeypatch.setattr(e2e, "_delete_test_owned_source_volume", delete_volume)
    monkeypatch.setattr(
        test_remote_function_schedule,
        "build_test_function_name",
        Mock(return_value="ex-heartbeat-test"),
    )

    with pytest.raises(pytest.fail.Exception, match="not ready"):
        test_remote_function_schedule.test_remote_function_registers_with_schedule()

    delete_function.assert_called_once_with("ex-heartbeat-test")
    delete_volume.assert_called_once_with(function, "ex-heartbeat-test")


def test_secret_deploy_failure_recovers_source_before_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = Mock()
    function.deploy.side_effect = RuntimeError("not ready")
    deployed = Mock()
    remote_function = Mock(return_value=function)
    remote_function.from_name.return_value = deployed
    delete_function = Mock()
    delete_volume = Mock()
    source_name = "fn-src-test-fn-secret-app"

    monkeypatch.setattr(
        test_function_secrets.chalkcompute, "RemoteFunction", remote_function
    )
    monkeypatch.setattr(
        test_function_secrets.Image, "debian_slim", Mock(return_value=object())
    )
    monkeypatch.setattr(
        test_function_secrets.Secret, "from_local_env", Mock(return_value=object())
    )
    monkeypatch.setattr(
        test_function_secrets,
        "_test_owned_source_volume_name",
        Mock(return_value=source_name),
    )
    monkeypatch.setattr(test_function_secrets, "_delete_function", delete_function)
    monkeypatch.setattr(
        test_function_secrets, "_delete_test_owned_source_volume", delete_volume
    )

    with pytest.raises(RuntimeError, match="not ready"):
        test_function_secrets.test_function_secret_from_local_env()

    name = remote_function.call_args.kwargs["name"]
    test_function_secrets._test_owned_source_volume_name.assert_called_once_with(
        deployed, name
    )
    delete_function.assert_called_once_with(name)
    delete_volume.assert_called_once_with(deployed, source_name)
