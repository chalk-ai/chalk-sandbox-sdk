"""Tests for the shared jittered backoff."""

from __future__ import annotations

import pytest

from chalkcompute import _backoff
from chalkcompute._backoff import Backoff


def test_waits_are_centered_on_a_growing_capped_step(monkeypatch) -> None:
    bounds: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        bounds.append((low, high))
        return (low + high) / 2

    monkeypatch.setattr(_backoff.random, "uniform", uniform)
    backoff = Backoff(0.25, 1.5)

    waits = [backoff.next_wait() for _ in range(5)]

    assert waits == [0.25, 0.5, 1.0, 1.5, 1.5]
    assert bounds == [(0.8 * wait, 1.2 * wait) for wait in waits]


@pytest.mark.parametrize("jitter", ["centered", "full"])
def test_waits_use_the_configured_multiplier(monkeypatch, jitter) -> None:
    bounds: list[tuple[float, float]] = []

    def uniform(low, high):
        bounds.append((low, high))
        return (low + high) / 2

    monkeypatch.setattr(_backoff.random, "uniform", uniform)
    backoff = Backoff(0.1, 1.0, jitter=jitter, multiplier=3.0)

    waits = [backoff.next_wait() for _ in range(5)]

    steps = [0.1, 0.3, 0.9, 1.0, 1.0]
    if jitter == "full":
        assert waits == pytest.approx([step / 2 for step in steps])
        assert [low for low, _ in bounds] == [0.0] * len(steps)
        assert [high for _, high in bounds] == pytest.approx(steps)
    else:
        assert waits == pytest.approx(steps)
        assert [low for low, _ in bounds] == pytest.approx([0.8 * s for s in steps])
        assert [high for _, high in bounds] == pytest.approx([1.2 * s for s in steps])


@pytest.mark.parametrize(
    ("jitter", "fraction", "expected"),
    [
        ("centered", 0.0, 0.8),
        ("centered", 1.0, 1.2),
        ("full", 0.0, 0.0),
        ("full", 1.0, 1.0),
    ],
)
def test_initial_wait_respects_the_capped_jitter_range(
    monkeypatch, jitter, fraction, expected
) -> None:
    monkeypatch.setattr(
        _backoff.random, "uniform", lambda low, high: low + fraction * (high - low)
    )
    backoff = Backoff(10.0, 1.0, jitter=jitter)

    assert [backoff.next_wait() for _ in range(3)] == [expected] * 3


@pytest.mark.parametrize(
    ("remaining", "expected"), [(0.3, 0.3), (5.0, 1.2), (-1.0, 0.0)]
)
@pytest.mark.parametrize("jitter", ["centered", "full"])
def test_wait_is_clipped_to_remaining_time(
    monkeypatch, jitter, remaining, expected
) -> None:
    monkeypatch.setattr(_backoff.random, "uniform", lambda low, high: high)

    if jitter == "full":
        expected = min(expected, 1.0)
    assert Backoff(1.0, 1.0, jitter=jitter).next_wait(remaining) == expected
