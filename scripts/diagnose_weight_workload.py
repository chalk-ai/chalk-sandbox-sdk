#!/usr/bin/env python3
"""Read-only Linux thread and CPU quota diagnostics; never claims compute.

Run inside the affected workload: python diagnose_weight_workload.py --pid 1.
JSON includes thread names and CPU ticks, never command lines or credentials.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import time


def read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError):
        return None


def parse_limit(value: str | None) -> int | None:
    try:
        return int(value) if value is not None and int(value) >= 0 else None
    except ValueError:
        return None


def parse_counters(value: str | None) -> dict[str, int]:
    result = {}
    for line in (value or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            number = parse_limit(parts[1])
            if number is not None:
                result[parts[0]] = number
    return result


def parse_cpu_max(value: str | None) -> float | None:
    if value is None:
        return None
    parts = value.split()
    if len(parts) != 2 or parts[0] == "max":
        return None
    try:
        quota, period = map(int, parts)
        return quota / period if quota > 0 and period > 0 else None
    except ValueError:
        return None


def cpu_context(pid: int, proc_root: Path, cgroup_root: Path) -> dict:
    membership = read(proc_root / str(pid) / "cgroup") or ""
    candidates = {cgroup_root}
    for line in membership.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            relative = Path(parts[2].lstrip("/"))
            if ".." not in relative.parts:
                current = cgroup_root / relative
                while current.is_relative_to(cgroup_root):
                    candidates.add(current)
                    if current == cgroup_root:
                        break
                    current = current.parent
    limits = []
    for path in sorted(candidates):
        maximum = read(path / "cpu.max")
        if maximum is not None:
            limits.append(
                {
                    "cpu_max": maximum,
                    "quota_cpus": parse_cpu_max(maximum),
                    "cpu_stat": parse_counters(read(path / "cpu.stat")),
                    "memory_max": parse_limit(read(path / "memory.max")),
                    "memory_current": parse_limit(read(path / "memory.current")),
                    "cpuset_cpus_effective": read(path / "cpuset.cpus.effective"),
                }
            )
    # Common cgroup v1 mount layouts. Negative quota means unlimited.
    v1 = []
    for path in (cgroup_root, cgroup_root / "cpu", cgroup_root / "cpu,cpuacct"):
        quota, period = (
            read(path / "cpu.cfs_quota_us"),
            read(path / "cpu.cfs_period_us"),
        )
        if quota is not None and period is not None:
            v1.append(
                {"path": str(path), "quota_cpus": parse_cpu_max(f"{quota} {period}")}
            )
    quotas = [
        item["quota_cpus"] for item in [*limits, *v1] if item["quota_cpus"] is not None
    ]
    try:
        affinity = sorted(os.sched_getaffinity(pid))
    except (AttributeError, OSError):
        affinity = None
    return {
        "host_logical_cpus": os.cpu_count(),
        "affinity_count": len(affinity) if affinity is not None else None,
        "minimum_visible_quota_cpus": min(quotas) if quotas else None,
        "cgroup_v2": limits,
        "cgroup_v1": v1,
    }


def threads(pid: int, proc_root: Path) -> dict[int, dict]:
    result = {}
    task_root = proc_root / str(pid) / "task"
    for task in task_root.iterdir():
        name, stat = read(task / "comm"), read(task / "stat")
        if name is None or stat is None:
            continue  # Threads can exit between directory and file reads.
        fields = stat[stat.rfind(")") + 1 :].split()
        try:
            result[int(task.name)] = {
                "name": name,
                "state": fields[0],
                "cpu_ticks": int(fields[11]) + int(fields[12]),
            }
        except (ValueError, IndexError):
            continue
    return result


def diagnose(
    pid: int, proc_root: Path, cgroup_root: Path, sample_seconds: float
) -> dict:
    before = threads(pid, proc_root)
    started = time.monotonic()
    if sample_seconds:
        time.sleep(sample_seconds)
    after = threads(pid, proc_root)
    elapsed = time.monotonic() - started
    groups = {}
    for tid, thread in after.items():
        group = groups.setdefault(
            thread["name"], {"count": 0, "states": Counter(), "cpu_ticks_delta": 0}
        )
        group["count"] += 1
        group["states"][thread["state"]] += 1
        if tid in before:
            group["cpu_ticks_delta"] += max(
                0, thread["cpu_ticks"] - before[tid]["cpu_ticks"]
            )
    return {
        "pid": pid,
        "thread_count": len(after),
        "sample_seconds": elapsed,
        "clock_ticks_per_second": os.sysconf("SC_CLK_TCK"),
        "thread_groups": dict(
            sorted(groups.items(), key=lambda item: (-item[1]["count"], item[0]))
        ),
        "cpu": cpu_context(pid, proc_root, cgroup_root),
        "limitations": "Thread names may be truncated. Quotas reflect readable cgroup ancestors only; CPU ticks omit threads created or exited during sampling.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, default=os.getpid())
    parser.add_argument("--sample-seconds", type=float, default=1)
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    parser.add_argument("--cgroup-root", type=Path, default=Path("/sys/fs/cgroup"))
    args = parser.parse_args()
    if args.pid < 1 or not 0 <= args.sample_seconds <= 10:
        parser.error("pid must be positive and sample-seconds between 0 and 10")
    print(
        json.dumps(
            diagnose(args.pid, args.proc_root, args.cgroup_root, args.sample_seconds),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
