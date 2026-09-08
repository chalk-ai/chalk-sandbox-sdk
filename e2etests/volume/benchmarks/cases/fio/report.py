"""Render FIO tables and compare saved provider baselines."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any

from .workload import RESULT_NAMES

CONFIG_KEYS = (
    "workload",
    "fio_size",
    "fio_jobs",
    "fio_small_files",
    "fio_small_file_size",
    "fio_direct",
    "sandbox_cpu",
    "sandbox_memory",
    "base_image",
    "fio_payload_sha256",
    "cache_state",
)
METRICS = (
    ("Seconds", "seconds", 1, "lower"),
    ("MiB/s", "fio_bw_bytes_per_sec", 1024**2, "higher"),
    ("IOPS", "fio_iops", 1, "higher"),
    ("p95 ms", "fio_clat_p95_ns", 1_000_000, "lower"),
)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"Invalid benchmark value: {value!r}")
    return float(value)


def _format(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _rows(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for benchmark in document["benchmarks"]:
        metadata = {**document.get("metadata", {}), **benchmark.get("metadata", {})}
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Benchmark must have a name")
        if name == "benchmark-status":
            if metadata.get("benchmark_error_count", 0):
                raise ValueError("Cannot report a failed benchmark as a baseline")
            continue
        if name in rows:
            raise ValueError(f"Duplicate benchmark: {name}")
        samples: list[float] = []
        measured_metadata: list[dict[str, Any]] = []
        for run in benchmark["runs"]:
            run_metadata = {**metadata, **run.get("metadata", {})}
            if run.get("values"):
                if measured_metadata and any(
                    measured_metadata[0].get(key) != run_metadata.get(key)
                    for key in (
                        *CONFIG_KEYS,
                        "volume_provider",
                        "fio_version",
                        "fio_io_bytes",
                    )
                ):
                    raise ValueError(f"Mixed configurations in {name}")
                for value in run["values"]:
                    sample = _number(value)
                    if sample is None:
                        raise ValueError(f"Missing timing for {name}")
                    samples.append(sample)
                    measured_metadata.append(run_metadata)
        if not samples:
            raise ValueError(f"Missing timings for {name}")
        row = {**measured_metadata[0], "seconds": mean(samples)}
        for _, key, _, _ in METRICS[1:]:
            values = [_number(item.get(key)) for item in measured_metadata]
            row[key] = (
                mean(value for value in values if value is not None)
                if all(value is not None for value in values)
                else None
            )
        rows[name] = row
    missing = set(RESULT_NAMES) - rows.keys()
    if missing:
        raise ValueError(f"Missing FIO workloads: {', '.join(sorted(missing))}")
    return rows


def _description(document: dict[str, Any]) -> str:
    metadata = document.get("metadata", {})
    keys = (
        "volume_provider",
        *CONFIG_KEYS,
        "provider_sdk_version",
        "provider_environment",
        "requested_region",
        "requested_cloud",
        "recorded_at",
    )
    return "\n".join(f"{key}: `{metadata[key]}`  " for key in keys if key in metadata)


def render_table(document: dict[str, Any]) -> str:
    rows = _rows(document)
    lines = [
        _description(document),
        "",
        "| Workload | Seconds | MiB/s | IOPS | p95 ms |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, row in rows.items():
        values = []
        for _, key, divisor, _ in METRICS:
            value = _number(row.get(key))
            values.append(_format(None if value is None else value / divisor))
        lines.append(f"| {name} | " + " | ".join(values) + " |")
    lines.extend(
        [
            "",
            "Persistence timings describe each provider's operation and are excluded from cross-provider ratios.",
        ]
    )
    return "\n".join(lines)


def compare(candidate: dict[str, Any], baseline: dict[str, Any]) -> str:
    candidate_rows, baseline_rows = _rows(candidate), _rows(baseline)
    labels = []
    for document in (candidate, baseline):
        provider = document.get("metadata", {}).get("volume_provider")
        if not isinstance(provider, str) or not provider:
            raise ValueError("Results must identify volume_provider")
        labels.append(provider.replace("|", "\\|").replace("\n", " "))
    for name in RESULT_NAMES:
        left, right = candidate_rows[name], baseline_rows[name]
        for key in (*CONFIG_KEYS, "fio_version", "fio_io_bytes"):
            if key not in left or key not in right or left[key] != right[key]:
                raise ValueError(
                    f"Incompatible {name}: {key} (candidate={left.get(key)!r}, baseline={right.get(key)!r})"
                )
        if left["fio_version"] == "unknown":
            raise ValueError("Cannot compare an unknown FIO version")
    lines = [
        _description(candidate),
        "",
        _description(baseline),
        "",
        "Ratio is candidate / baseline. Lower is better for seconds and latency, higher for throughput and IOPS.",
        "",
        f"| Workload | Metric | Better | Candidate ({labels[0]}) | Baseline ({labels[1]}) | Candidate / baseline |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for name in RESULT_NAMES:
        for label, key, divisor, better in METRICS:
            left = _number(candidate_rows[name].get(key))
            right = _number(baseline_rows[name].get(key))
            ratio = (
                left / right
                if left is not None and right is not None and right > 0
                else None
            )
            left_text = _format(None if left is None else left / divisor)
            right_text = _format(None if right is None else right / divisor)
            lines.append(
                f"| {name} | {label} | {better} | {left_text} | {right_text} | {_format(ratio)} |"
            )
    lines.extend(
        [
            "",
            "Persistence timings are available in the individual reports. Hardware and backend cache state may differ.",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        table = compare(
            json.loads(args.candidate.read_text()),
            json.loads(args.baseline.read_text()),
        )
    except (ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(table)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(table + "\n")


if __name__ == "__main__":
    main()
