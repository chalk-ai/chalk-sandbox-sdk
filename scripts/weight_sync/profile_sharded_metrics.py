"""Owned guest-tree measurements; no scans of unrelated cache or model data."""

from __future__ import annotations

import os
import resource
import sys
import time
from pathlib import Path

MAX_FIXTURE_BYTES = 256 * 1024**2
REMOTE_FILE_RESERVE = 48 * 1024**2


def tree_bytes(root):
    stats = [p.stat() for p in root.rglob("*") if p.is_file() and not p.is_symlink()]
    return {
        "logical": sum(s.st_size for s in stats),
        "allocated": sum(s.st_blocks * 512 for s in stats),
    }


def guest_cgroup():
    membership = Path("/proc/self/cgroup")
    root = Path("/sys/fs/cgroup")
    if not membership.exists() or not root.exists():
        return {"available": False}
    paths = [
        line.split(":", 2)[2]
        for line in membership.read_text().splitlines()
        if line.startswith("0::")
    ]
    if len(paths) != 1:
        return {"available": False}
    current = root / paths[0].lstrip("/")
    values = []
    while current.is_relative_to(root):
        item = {"path": str(current)}
        for field in ("memory.current", "memory.peak", "memory.max", "cpu.max"):
            file = current / field
            if file.exists():
                item[field] = file.read_text().strip()
        values.append(item)
        if current == root:
            break
        current = current.parent
    return {
        "available": any("memory.current" in item for item in values),
        "ancestors": values,
    }


class Measurements:
    def __init__(self, root):
        self.root, self.samples = root, []
        self.logical = self.allocated = 0

    def sample(self, stage):
        files = tree_bytes(self.root)
        self.logical = max(self.logical, files["logical"])
        self.allocated = max(self.allocated, files["allocated"])
        if max(files.values()) + REMOTE_FILE_RESERVE > MAX_FIXTURE_BYTES:
            raise RuntimeError(
                "Owned guest files plus remote fixture reserve exceed256MiB"
            )
        filesystem = os.statvfs(self.root)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.samples.append(
            {
                "stage": stage,
                "time": time.time(),
                "owned_files": files,
                "process_peak_rss_bytes": rss
                if sys.platform == "darwin"
                else rss * 1024,
                "filesystem_free_bytes": filesystem.f_bavail * filesystem.f_frsize,
                "cache_roots": {
                    name: tree_bytes(self.root / name)
                    for name in ("home", "cache", "tmp")
                },
                "guest_cgroup": guest_cgroup(),
            }
        )

    def result(self):
        return {
            "samples": self.samples,
            "peak_logical_owned_file_bytes": self.logical,
            "peak_allocated_owned_file_bytes": self.allocated,
            "remote_fixture_reserve_bytes": REMOTE_FILE_RESERVE,
            "maximum_total_fixture_bytes": MAX_FIXTURE_BYTES,
            "disk_sampling_scope": "controlled stage boundaries, owned HOME/XDG_CACHE_HOME/TMPDIR; filesystem free-byte deltas are not exclusive attribution",
        }
