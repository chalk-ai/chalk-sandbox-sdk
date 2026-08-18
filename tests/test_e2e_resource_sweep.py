"""Guards for the e2e suite's stale-resource sweep.

The sweep deletes scaling groups, so the pattern it matches on has to be exactly
as narrow as intended: build-scoped names only, never the fixed example names the
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
    "ex-trivial-double-sg",
    "ex-volume-triple-sg",
    # Unsuffixed generator names, i.e. what a local run without a build id creates.
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
    ],
)
def test_sweep_matches_build_scoped_names(name: str) -> None:
    assert SCOPED_NAME_RE.match(name) is not None


def test_build_scoped_name_is_sweepable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever ``build_scoped_name`` emits in CI must be reapable."""
    monkeypatch.setenv("BUILDKITE_BUILD_ID", "01a011e8-9f3c-4c2a-8d5e-6b1f0a2c3d4e")
    scoped = build_scoped_name("ex-count-up")
    assert scoped == "ex-count-up-01a011e8"
    assert SCOPED_NAME_RE.match(scoped) is not None


def test_build_scoped_name_falls_back_to_base_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local runs get the bare name, which the sweep never touches."""
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
