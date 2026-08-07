"""Tests for the process-wide shared gRPC channel registry."""

from __future__ import annotations

import json

import pytest

import chalkcompute._channel as channel_mod
from chalkcompute._channel import default_channel_options, get_channel


@pytest.fixture(autouse=True)
def _clean_registry():
    channel_mod.close_all()
    yield
    channel_mod.close_all()


def test_same_key_returns_same_channel() -> None:
    a = get_channel("localhost:50051", False)
    b = get_channel("localhost:50051", False)
    assert a is b


def test_distinct_targets_get_distinct_channels() -> None:
    a = get_channel("localhost:50051", False)
    b = get_channel("localhost:50052", False)
    assert a is not b


def test_tls_flag_is_part_of_the_key() -> None:
    a = get_channel("localhost:50051", False)
    b = get_channel("localhost:50051", True)
    assert a is not b


def test_fork_drops_cached_channels_without_closing(monkeypatch) -> None:
    parent = get_channel("localhost:50051", False)
    closed: list[bool] = []
    monkeypatch.setattr(parent, "close", lambda: closed.append(True), raising=False)

    # Simulate a fork: the registry must hand out a NEW channel in the child
    # and must not close the parent's channel (its sockets aren't ours).
    monkeypatch.setattr(channel_mod.os, "getpid", lambda: channel_mod._owner_pid + 1)
    child = get_channel("localhost:50051", False)

    assert child is not parent
    assert closed == []


def test_close_all_empties_registry() -> None:
    a = get_channel("localhost:50051", False)
    channel_mod.close_all()
    b = get_channel("localhost:50051", False)
    assert a is not b


def test_default_options_include_keepalive_and_retries() -> None:
    options = dict(default_channel_options())
    assert options["grpc.keepalive_time_ms"] == 60_000
    assert options["grpc.keepalive_permit_without_calls"] == 1
    assert options["grpc.max_receive_message_length"] == -1
    service_config = json.loads(options["grpc.service_config"])
    retry = service_config["methodConfig"][0]["retryPolicy"]
    assert retry["retryableStatusCodes"] == ["UNAVAILABLE"]
    assert retry["maxAttempts"] == 5


def test_dead_target_construction_is_safe() -> None:
    # gRPC dials lazily; building a channel to an unreachable target must not
    # raise or block. (Unit tests rely on this everywhere.)
    channel = get_channel("localhost:1", False)
    assert channel is not None
