"""Guards for the e2e suite's stale-resource sweep.

The sweep deletes scaling groups, so the pattern it matches on has to be exactly
as narrow as intended: CI-scoped names only, never the fixed example names the
other e2e tests reuse and never a developer's local deploy.
"""

from __future__ import annotations

from datetime import timezone

import pytest

from e2etests._ci_resources import SCOPED_NAME_RE, build_scoped_name, parse_rfc3339

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
