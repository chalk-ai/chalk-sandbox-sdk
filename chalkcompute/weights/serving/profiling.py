"""Opt-in bounded timing telemetry; CUDA intervals are stream elapsed, not GPU busy time."""

from __future__ import annotations

import math
import os
import re
import time
from contextlib import contextmanager

HOST_PHASES = (
    "manifest",
    "verify",
    "read_checksum",
    "decode",
    "h2d_host",
    "dense_load_host",
    "sparse_load_host",
    "synchronize_host",
)
STREAM_PHASES = ("h2d_host", "dense_load_host", "sparse_load_host")
COUNTS = ("payload_bytes", "value_bytes", "index_bytes", "tensors", "sparse_batches")


class WorkerTimer:
    def __init__(self, enabled: bool, torch, device):
        self.enabled, self.torch, self.device = enabled, torch, device
        self.host = dict.fromkeys(HOST_PHASES, 0.0)
        self.counts = dict.fromkeys(COUNTS, 0)
        self.events = []

    @contextmanager
    def phase(self, name: str):
        if not self.enabled:
            yield
            return
        started = time.perf_counter()
        pair = None
        if self.device.type == "cuda" and name in STREAM_PHASES:
            # Use the actual worker device/current stream. Events are read only
            # after the existing final device synchronize, never per tensor.
            with self.torch.cuda.device(self.device):
                stream = self.torch.cuda.current_stream(self.device)
                pair = (
                    self.torch.cuda.Event(enable_timing=True),
                    self.torch.cuda.Event(enable_timing=True),
                )
                pair[0].record(stream)
        try:
            yield
        finally:
            if pair is not None:
                with self.torch.cuda.device(self.device):
                    pair[1].record(stream)
                self.events.append((name, pair))
            self.host[name] += (time.perf_counter() - started) * 1000

    def result(self, worker, version: int):
        elapsed = {}
        for name, (start, end) in self.events:
            elapsed[name] = elapsed.get(name, 0.0) + start.elapsed_time(end)
        return {
            "schema_version": 1,
            "version": version,
            "rank": worker.rank,
            "local_rank": worker.local_rank,
            "worker_pid": os.getpid(),
            "device": str(self.device),
            "host_ms": self.host,
            "stream_elapsed_ms": elapsed,
            "counts": self.counts,
        }


def validate_profiles(results: list, version: int, workers: int) -> list[dict]:
    """Accept only finite fixed-schema telemetry from every distinct global rank."""
    profiles = []
    ranks = set()
    for result in results:
        profile = result.get("profile")
        if not isinstance(profile, dict) or set(profile) != {
            "schema_version",
            "version",
            "rank",
            "local_rank",
            "worker_pid",
            "device",
            "host_ms",
            "stream_elapsed_ms",
            "counts",
        }:
            raise ValueError("Invalid worker profile schema")
        if (
            type(profile["schema_version"]) is not int
            or type(profile["version"]) is not int
            or profile["schema_version"] != 1
            or profile["version"] != version
        ):
            raise ValueError("Unexpected worker profile version")
        for name, minimum in (("rank", 0), ("local_rank", 0), ("worker_pid", 1)):
            if type(profile[name]) is not int or profile[name] < minimum:
                raise ValueError("Invalid worker profile identity")
        if profile["rank"] in ranks or not 0 <= profile["rank"] < workers:
            raise ValueError("Duplicate or unexpected worker rank")
        ranks.add(profile["rank"])
        if not isinstance(profile["device"], str) or not re.fullmatch(
            r"cpu|cuda:\d+", profile["device"]
        ):
            raise ValueError("Invalid worker device")
        for field, keys, exact in (
            ("host_ms", HOST_PHASES, True),
            ("stream_elapsed_ms", STREAM_PHASES, False),
        ):
            values = profile[field]
            if not isinstance(values, dict) or (
                set(values) != set(keys) if exact else not set(values) <= set(keys)
            ):
                raise ValueError("Invalid worker timing phases")
            if any(
                type(v) not in (int, float) or not math.isfinite(v) or v < 0
                for v in values.values()
            ):
                raise ValueError("Worker timings must be finite and nonnegative")
        counts = profile["counts"]
        if (
            not isinstance(counts, dict)
            or set(counts) != set(COUNTS)
            or any(type(value) is not int or value < 0 for value in counts.values())
        ):
            raise ValueError("Invalid worker profile counts")
        profiles.append(profile)
    if ranks != set(range(workers)):
        raise ValueError("Missing worker profile ranks")
    return sorted(profiles, key=lambda p: p["rank"])
