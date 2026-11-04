"""Tests for the shared jittered backoff."""

from __future__ import annotations

import pytest

from chalkcompute import _backoff
from chalkcompute._backoff import Backoff


def test_waits_are_drawn_up_to_a_growing_capped_step(monkeypatch) -> None:
    bounds: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        bounds.append((low, high))
        return high

    monkeypatch.setattr(_backoff.random, "uniform", uniform)
    backoff = Backoff(0.25, 1.5)

    waits = [backoff.next_wait() for _ in range(5)]

    assert waits == [0.25, 0.5, 1.0, 1.5, 1.5]
    assert bounds == [(0.0, wait) for wait in waits]


def test_multiplier_sets_step_growth(monkeypatch) -> None:
    monkeypatch.setattr(_backoff.random, "uniform", lambda low, high: high)
    backoff = Backoff(1.0, 100.0, multiplier=3.0)

    assert [backoff.next_wait() for _ in range(3)] == [1.0, 3.0, 9.0]


def test_initial_wait_respects_the_maximum(monkeypatch) -> None:
    monkeypatch.setattr(_backoff.random, "uniform", lambda low, high: high)
    backoff = Backoff(10.0, 1.0)

    assert [backoff.next_wait() for _ in range(3)] == [1.0, 1.0, 1.0]


def test_mean_preserving_jitter_is_centered_on_each_capped_step(monkeypatch) -> None:
    bounds = []

    def uniform(low: float, high: float) -> float:
        bounds.append((low, high))
        return (low + high) / 2

    monkeypatch.setattr(_backoff.random, "uniform", uniform)
    backoff = Backoff(0.25, 1.0, jitter="mean_preserving")

    assert [backoff.next_wait() for _ in range(4)] == [0.25, 0.5, 1.0, 1.0]
    assert bounds == [(0.125, 0.375), (0.25, 0.75), (0.5, 1.5), (0.5, 1.5)]


def test_mean_preserving_jitter_clamps_the_initial_nominal_step(monkeypatch) -> None:
    monkeypatch.setattr(_backoff.random, "uniform", lambda low, high: high)

    assert Backoff(10.0, 1.0, jitter="mean_preserving").next_wait() == 1.5


@pytest.mark.parametrize(
    ("remaining", "expected"), [(0.3, 0.3), (5.0, 1.5), (-1.0, 0.0)]
)
def test_mean_preserving_jitter_is_clipped_to_remaining_time(
    monkeypatch, remaining, expected
) -> None:
    monkeypatch.setattr(_backoff.random, "uniform", lambda low, high: high)

    assert Backoff(1.0, 1.0, jitter="mean_preserving").next_wait(remaining) == expected


def test_waits_are_random_within_each_step() -> None:
    backoff = Backoff(1.0, 1.0)

    waits = [backoff.next_wait() for _ in range(200)]

    assert all(0.0 <= wait <= 1.0 for wait in waits)
    assert len(set(waits)) > 1


@pytest.mark.parametrize(
    ("remaining", "expected"), [(0.3, 0.3), (5.0, 1.0), (-1.0, 0.0)]
)
def test_wait_is_clipped_to_remaining_time(monkeypatch, remaining, expected) -> None:
    monkeypatch.setattr(_backoff.random, "uniform", lambda low, high: high)

    assert Backoff(1.0, 1.0).next_wait(remaining) == expected
