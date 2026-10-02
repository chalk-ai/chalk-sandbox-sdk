"""Guards for the e2e suite's stale-resource sweep.

The sweep deletes scaling groups, so the pattern it matches on has to be exactly
as narrow as intended: CI-scoped names only, never the fixed example names the
other e2e tests reuse and never a developer's local deploy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from e2etests._ci_resources import SCOPED_NAME_RE, build_scoped_name, parse_rfc3339

pytest_plugins = ("pytester",)

# Every fixed function/scaling-group name the e2e suite deploys and reuses across
# builds.  The sweep must not match any of them.
FIXED_E2E_NAMES = [
    "ex-add",
    "ex-async-add",
    "ex-nested-gather-add",
    "ex-add-rate-limited",
    "ex-slow-add",
    "ex-embed",
    "ex-async-embed",
    "ex-orchestrate",
    "ex-classify-image",
    "ex-policy-check",
    "ex-heartbeat",
    "ex-function-sandbox",
    "ex-trivial-double-sg",
    "ex-volume-triple-sg",
    "ex-handler-affine-sg",
    "ex-combined-affine-sg",
    # Unsuffixed generator names, i.e. what a local run outside CI creates.
    "ex-count-up",
    "ex-async-count-up",
]


@pytest.mark.parametrize("name", FIXED_E2E_NAMES)
def test_sweep_ignores_fixed_names(name: str) -> None:
    assert SCOPED_NAME_RE.match(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "ex-count-up-01a011e8",
        "ex-async-count-up-01a011e8",
        "ex-add-deadbeef",
        "ex-count-up-44a136e21fc4",
        "ex-combined-affine-sg-44a136e21fc4",
    ],
)
def test_sweep_matches_build_scoped_names(name: str) -> None:
    assert SCOPED_NAME_RE.match(name) is not None


def test_build_scoped_name_is_sweepable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever ``build_scoped_name`` emits in CI must be reapable."""
    monkeypatch.setenv("BUILDKITE_JOB_ID", "01a01268-66f7-41d5-9ab4-7aefbc944c06")
    scoped = build_scoped_name("ex-count-up")
    assert scoped == "ex-count-up-44a136e21fc4"
    assert SCOPED_NAME_RE.match(scoped) is not None


def test_build_scoped_name_isolates_jobs_with_the_same_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent scheduled and regular jobs can share the first eight digits."""
    monkeypatch.setenv("BUILDKITE_JOB_ID", "01a0e8f5-a185-4d24-ad06-aec87742a894")
    scheduled = build_scoped_name("ex-combined-affine-sg")
    assert build_scoped_name("ex-combined-affine-sg") == scheduled

    monkeypatch.setenv("BUILDKITE_JOB_ID", "01a0e8f5-c00e-402f-8c3c-d53e3b58444f")
    assert build_scoped_name("ex-combined-affine-sg") != scheduled


def test_build_scoped_name_prefers_job_over_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry shares its build id but not its job id, so the job id must win.

    Otherwise the retry redeploys the name the first attempt's teardown deleted.
    """
    monkeypatch.setenv("BUILDKITE_BUILD_ID", "01a01253-4bb4-4ec7-9680-cfe878d36cfd")
    monkeypatch.setenv("BUILDKITE_JOB_ID", "01a01268-66f7-41d5-9ab4-7aefbc944c06")
    assert build_scoped_name("ex-count-up") == "ex-count-up-44a136e21fc4"


def test_build_scoped_name_falls_back_to_build_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a job id, build scoping still isolates the run."""
    monkeypatch.delenv("BUILDKITE_JOB_ID", raising=False)
    monkeypatch.setenv("BUILDKITE_BUILD_ID", "01a011e8-9f3c-4c2a-8d5e-6b1f0a2c3d4e")
    scoped = build_scoped_name("ex-count-up")
    assert scoped == "ex-count-up-58a9ff37128f"
    assert SCOPED_NAME_RE.match(scoped) is not None


def test_build_scoped_name_falls_back_to_base_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local runs get the bare name, which the sweep never touches."""
    monkeypatch.delenv("BUILDKITE_JOB_ID", raising=False)
    monkeypatch.delenv("BUILDKITE_BUILD_ID", raising=False)
    assert build_scoped_name("ex-count-up") == "ex-count-up"


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-17T21:00:00Z",
        "2026-08-17T21:00:00.123456Z",
        # proto3 JSON may emit nanosecond precision, which datetime cannot parse.
        "2026-08-17T21:00:00.123456789Z",
        "2026-08-17T21:00:00+00:00",
    ],
)
def test_parse_rfc3339_handles_proto_json_timestamps(value: str) -> None:
    parsed = parse_rfc3339(value)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.astimezone(timezone.utc).hour == 21


@pytest.mark.parametrize("value", [None, "", "not-a-timestamp"])
def test_parse_rfc3339_returns_none_for_unusable_values(value: str | None) -> None:
    """An undateable group is left alone rather than assumed stale."""
    assert parse_rfc3339(value) is None


def test_sweep_routes_by_model_metadata_not_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from e2etests import conftest

    old = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    recent = datetime.now(timezone.utc).isoformat()
    groups = [
        {
            "name": "ex-any-new-model-sg-44a136e21fc4",
            "id": "model-id",
            "createdAt": old,
            "metadata": {"model_name": "ex-any-new-model-44a136e21fc4"},
        },
        {"name": "ex-count-up-44a136e21fc4", "id": "function-id", "createdAt": old},
        {
            "name": "ex-volume-triple-sg-deadbeef1234",
            "id": "recent-id",
            "createdAt": recent,
            "metadata": {"model_name": "ex-volume-triple-deadbeef1234"},
        },
    ]
    calls: list[tuple[str, str, dict]] = []

    def rpc(service: str, method: str, body: dict, headers: dict, timeout: float = 30):
        calls.append((service, method, body))
        return SimpleNamespace(status=200, text=lambda: "")

    monkeypatch.setattr(conftest, "_get_access_token", lambda: "test-token")
    monkeypatch.setattr(conftest, "_iter_scaling_groups", lambda headers: iter(groups))
    monkeypatch.setattr(conftest, "_connect_rpc", rpc)
    conftest._sweep_stale_scoped_scaling_groups()

    assert calls == [
        (
            "chalk.modeldeployment.v1.ModelDeploymentService",
            "DeleteModelScalingGroup",
            {"modelScalingGroupId": "model-id"},
        ),
        (
            "chalk.scalinggroup.v1.ScalingGroupManagerService",
            "DeleteScalingGroup",
            {"id": "function-id"},
        ),
    ]


def test_model_delete_reports_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from e2etests import conftest

    monkeypatch.setattr(conftest, "_get_access_token", lambda: "test-token")
    monkeypatch.setattr(
        conftest,
        "_connect_rpc",
        lambda *args, **kwargs: SimpleNamespace(
            status=400, text=lambda: "model-owned group cannot be deleted here"
        ),
    )
    with pytest.raises(RuntimeError, match="model-owned group cannot be deleted here"):
        conftest._delete_model_scaling_group("ex-trivial-double-sg-44a136e21fc4")


@pytest.mark.parametrize("status, expected", [(200, True), (404, False)])
def test_delete_treats_not_found_as_absent(
    monkeypatch: pytest.MonkeyPatch, status: int, expected: bool
) -> None:
    from e2etests import conftest

    monkeypatch.setattr(
        conftest,
        "_connect_rpc",
        lambda *args, **kwargs: SimpleNamespace(status=status, text=lambda: ""),
    )
    assert (
        conftest._delete_group("service", "DeleteScalingGroup", {"id": "x"}, {})
        is expected
    )


def test_sweep_fails_when_stale_group_cannot_be_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from e2etests import conftest

    old = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    monkeypatch.setattr(conftest, "_get_access_token", lambda: "test-token")
    monkeypatch.setattr(
        conftest,
        "_iter_scaling_groups",
        lambda headers: iter(
            [
                {
                    "name": "ex-trivial-double-sg-44a136e21fc4",
                    "id": "model-id",
                    "createdAt": old,
                    "metadata": {"model_name": "ex-trivial-double-44a136e21fc4"},
                }
            ]
        ),
    )
    monkeypatch.setattr(
        conftest,
        "_connect_rpc",
        lambda *args, **kwargs: SimpleNamespace(
            status=400, text=lambda: "deployment is busy"
        ),
    )
    with pytest.raises(
        RuntimeError, match="1 stale scaling-group cleanup attempts failed"
    ):
        conftest._sweep_stale_scoped_scaling_groups()


def test_class_finalizer_runs_when_setup_fails(pytester: pytest.Pytester) -> None:
    """Exercise pytest's real class fixture and xunit setup order."""
    events = pytester.path / "cleanup-events.txt"
    repo_root = Path(__file__).resolve().parents[1]
    pytester.makepyfile(
        test_lifecycle=f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(repo_root)!r})
from e2etests.test_model_scaling_group import cleanup_model_test
import e2etests.test_model_scaling_group as module

def record_cleanup(group, model):
    with Path({str(events)!r}).open("a") as stream:
        stream.write(f"cleanup {{group}} {{model}}\\n")

module._cleanup_model_test = record_cleanup

class TestFailedSetup:
    _sg_name = "test-group"
    _model_name = "test-model"

    @classmethod
    def setup_class(cls):
        with Path({str(events)!r}).open("a") as stream:
            stream.write("setup\\n")
        raise RuntimeError("setup failed")

    def test_case(self):
        pass
"""
    )
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(errors=1)
    assert events.read_text().splitlines() == [
        "setup",
        "cleanup test-group test-model",
    ]


def test_class_finalizer_reports_cleanup_failure(pytester: pytest.Pytester) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    pytester.makepyfile(
        test_cleanup_failure=f"""
import sys
sys.path.insert(0, {str(repo_root)!r})
from e2etests.test_model_scaling_group import cleanup_model_test
import e2etests.test_model_scaling_group as module

def fail_cleanup(group, model):
    raise RuntimeError("group deletion failed")

module._cleanup_model_test = fail_cleanup

class TestCleanupFailure:
    _sg_name = "test-group"
    _model_name = "test-model"

    @classmethod
    def setup_class(cls):
        raise RuntimeError("setup failed")

    def test_case(self):
        pass
"""
    )
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(["*setup failed*", "*group deletion failed*"])


def test_model_cleanup_deletes_group_before_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from e2etests import test_model_scaling_group as module

    calls: list[str] = []
    monkeypatch.setattr(
        module, "_delete_model_scaling_group", lambda name: calls.append("group")
    )
    monkeypatch.setattr(
        module, "_delete_model_registration", lambda name: calls.append("model")
    )
    module._cleanup_model_test("group-name", "model-name")
    assert calls == ["group", "model"]


def test_failed_group_delete_keeps_model_registration_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from e2etests import test_model_scaling_group as module

    removed_models: list[str] = []
    monkeypatch.setattr(
        module,
        "_delete_model_scaling_group",
        lambda name: (_ for _ in ()).throw(RuntimeError("delete failed")),
    )
    monkeypatch.setattr(
        module,
        "_delete_model_registration",
        lambda name: removed_models.append(name),
    )

    with pytest.raises(RuntimeError, match="delete failed"):
        module._cleanup_model_test("group-name", "model-name")
    assert removed_models == []


@pytest.mark.parametrize("strict", [True, False])
def test_failed_group_sweep_does_not_delete_models(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], strict: bool
) -> None:
    from e2etests import conftest

    removed_models: list[str] = []
    monkeypatch.setattr(conftest, "CHALK_CLIENT_ID", "test-client")
    monkeypatch.setattr(conftest, "CHALK_API_SERVER", "https://example.test")
    if strict:
        monkeypatch.setenv("CHALK_E2E_STRICT_CLEANUP", "1")
    else:
        monkeypatch.delenv("CHALK_E2E_STRICT_CLEANUP", raising=False)
    monkeypatch.setattr(
        conftest,
        "_sweep_stale_scoped_scaling_groups",
        lambda: (_ for _ in ()).throw(RuntimeError("group delete failed")),
    )
    monkeypatch.setattr(
        conftest,
        "_sweep_stale_scoped_models",
        lambda: removed_models.append("called"),
    )

    if strict:
        with pytest.raises(pytest.fail.Exception, match="group delete failed"):
            conftest.sweep_stale_ci_resources.__wrapped__()
    else:
        conftest.sweep_stale_ci_resources.__wrapped__()
        assert "warning: stale scaling-group cleanup failed" in capsys.readouterr().out
    assert removed_models == []


@pytest.mark.parametrize("code", ["not_found", "internal"])
def test_model_delete_only_ignores_not_found(code: str) -> None:
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    from e2etests import conftest

    calls: list[str] = []
    stub = SimpleNamespace(delete_model="delete")

    def call(method: str, request: object):
        calls.append(method)
        error_code = Code.NOT_FOUND if code == "not_found" else Code.INTERNAL
        raise ConnectError(error_code, "delete failed")

    client = SimpleNamespace(call=call)
    if code == "internal":
        with pytest.raises(ConnectError, match="delete failed"):
            conftest._delete_model_with_client(client, stub, "model-name")
    else:
        assert conftest._delete_model_with_client(client, stub, "model-name") is False
    assert calls == ["delete"]
