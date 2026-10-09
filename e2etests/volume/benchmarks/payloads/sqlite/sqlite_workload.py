#!/usr/bin/env python3
"""Run SQLite write, read, journal, and rename workloads on a mounted Volume."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import sqlite3
import sys
import time
from typing import Any, Callable

ROW_PAYLOAD_BYTES = 2048
BATCH_ROWS = 50_000
POINT_READS = 10_000
SMALL_TRANSACTIONS = 200
RENAME_CYCLES = 50
RENAME_ROWS = 1_000


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mount-path", required=True)
    parser.add_argument("--mode", choices=("write", "verify"), required=True)
    parser.add_argument("--size-mib", type=positive_int, default=5120)
    parser.add_argument("--result", required=True)
    return parser.parse_args()


def connect(path: Path) -> sqlite3.Connection:
    # Autocommit so BEGIN and COMMIT are explicit
    return sqlite3.connect(path, isolation_level=None)


def file_bytes(path: Path) -> int:
    return sum(
        p.stat().st_size
        for p in (path, Path(f"{path}-wal"))
        if p.exists()
    )


def insert(db: Path, target_bytes: int) -> dict[str, Any]:
    conn = connect(db)
    conn.execute("PRAGMA page_size = 4096")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute(
        "CREATE TABLE stress_data ("
        "id INTEGER PRIMARY KEY, ts REAL, hash TEXT, payload BLOB)"
    )
    batches = 0
    while file_bytes(db) < target_bytes:
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO stress_data (ts, hash, payload) VALUES (?, ?, ?)",
            (
                (time.time(), os.urandom(16).hex(), os.urandom(ROW_PAYLOAD_BYTES))
                for _ in range(BATCH_ROWS)
            ),
        )
        conn.execute("COMMIT")
        batches += 1
        print(f"batch {batches} size {file_bytes(db) / 1024**3:.2f} GiB", flush=True)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    rows = conn.execute("SELECT COUNT(*) FROM stress_data").fetchone()[0]
    conn.close()
    return {"rows": rows, "db_bytes": db.stat().st_size}


def read(db: Path) -> dict[str, Any]:
    conn = connect(db)
    max_id = conn.execute("SELECT MAX(id) FROM stress_data").fetchone()[0]
    matched = conn.execute(
        "SELECT COUNT(*) FROM stress_data WHERE hash LIKE 'a%'"
    ).fetchone()[0]
    rng = random.Random(1)
    for _ in range(POINT_READS):
        row = conn.execute(
            "SELECT length(payload) FROM stress_data WHERE id = ?",
            (rng.randint(1, max_id),),
        ).fetchone()
        if row is None or row[0] != ROW_PAYLOAD_BYTES:
            raise RuntimeError(f"point read returned {row!r}")
    conn.close()
    return {"scan_matched_rows": matched, "point_reads": POINT_READS}


def journal_delete(db: Path) -> dict[str, Any]:
    # Each transaction creates, writes, and unlinks a rollback journal
    conn = connect(db)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA synchronous = FULL")
    for index in range(SMALL_TRANSACTIONS):
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO stress_data (ts, hash, payload) VALUES (?, ?, ?)",
            (time.time(), f"journal-{index}", os.urandom(ROW_PAYLOAD_BYTES)),
        )
        conn.execute("COMMIT")
        if index % 20 == 0:
            print(f"journal transaction {index}", flush=True)
    conn.close()
    return {"transactions": SMALL_TRANSACTIONS}


def vacuum_rename(db: Path) -> dict[str, Any]:
    # Atomic replace of the live database with a compacted copy
    tmp = db.with_name(f"{db.name}.vacuum")
    conn = connect(db)
    conn.execute("VACUUM INTO ?", (str(tmp),))
    conn.close()
    os.replace(tmp, db)
    return {"db_bytes": db.stat().st_size}


def rename_cycles(db: Path) -> list[float]:
    # Many small write then rename over a live file cycles
    target = db.with_name("rename-target.db")
    tmp = db.with_name("rename-target.db.tmp")
    durations = []
    for cycle in range(RENAME_CYCLES):
        started = time.monotonic()
        conn = connect(tmp)
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, cycle INT, v BLOB)")
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO t (cycle, v) VALUES (?, ?)",
            ((cycle, os.urandom(256)) for _ in range(RENAME_ROWS)),
        )
        conn.execute("COMMIT")
        conn.close()
        os.replace(tmp, target)
        conn = connect(target)
        got = conn.execute("SELECT COUNT(*), MIN(cycle) FROM t").fetchone()
        ok = conn.execute("PRAGMA quick_check").fetchone()[0]
        conn.close()
        if got != (RENAME_ROWS, cycle) or ok != "ok":
            raise RuntimeError(f"cycle {cycle} read {got!r} quick_check {ok!r}")
        durations.append(time.monotonic() - started)
    return durations


def nested_dir_rename(mount: Path) -> list[float]:
    # Rename a top level folder and a middle folder above live databases
    root = mount / "nested-a"
    leaves = [Path("b1/c/d"), Path("b2/c/d"), Path("b2/c/e")]
    for leaf in leaves:
        (root / leaf).mkdir(parents=True)
        conn = connect(root / leaf / "app.db")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, leaf TEXT)")
        conn.execute("INSERT INTO t (leaf) VALUES (?)", (str(leaf),))
        conn.close()

    durations = []
    top = root
    for cycle in range(RENAME_CYCLES):
        started = time.monotonic()
        renamed_top = mount / ("nested-a" if cycle % 2 else "nested-b")
        os.rename(top, renamed_top)
        top = renamed_top
        os.rename(top / "b2" / "c", top / "b2" / "c-renamed")
        os.rename(top / "b2" / "c-renamed", top / "b2" / "c")
        for leaf in leaves:
            conn = connect(top / leaf / "app.db")
            conn.execute("INSERT INTO t (leaf) VALUES (?)", (str(leaf),))
            got = conn.execute("SELECT COUNT(*), MIN(leaf) FROM t").fetchone()
            ok = conn.execute("PRAGMA quick_check").fetchone()[0]
            conn.close()
            if got != (cycle + 2, str(leaf)) or ok != "ok":
                raise RuntimeError(
                    f"cycle {cycle} {top / leaf} read {got!r} quick_check {ok!r}"
                )
        durations.append(time.monotonic() - started)
    return durations


def integrity(db: Path) -> dict[str, Any]:
    conn = connect(db)
    result = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    rows = conn.execute("SELECT COUNT(*) FROM stress_data").fetchone()[0]
    conn.close()
    if result != ["ok"]:
        raise RuntimeError(f"integrity_check: {result[:10]}")
    return {"rows": rows}


def nested_integrity(mount: Path) -> dict[str, Any]:
    paths = sorted(mount.glob("nested-*/**/app.db"))
    if len(paths) != 3:
        raise RuntimeError(f"expected 3 nested databases, found {paths}")
    for path in paths:
        conn = connect(path)
        ok = conn.execute("PRAGMA quick_check").fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.close()
        if ok != "ok" or rows != RENAME_CYCLES + 1:
            raise RuntimeError(f"{path} rows {rows} quick_check {ok!r}")
    return {"databases": len(paths)}


def wal_reopen(db: Path) -> dict[str, Any]:
    # WAL needs a shared memory file mapped on the fresh mount
    conn = connect(db)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO stress_data (ts, hash, payload) VALUES (?, 'verify', ?)",
        (time.time(), os.urandom(ROW_PAYLOAD_BYTES)),
    )
    conn.execute("COMMIT")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.close()
    return {}


def main() -> int:
    args = parse_args()
    mount = Path(args.mount_path)
    db = mount / "stress.db"
    phases: dict[str, Any] = {}
    result: dict[str, Any] = {"mode": args.mode, "phases": phases}

    def run(name: str, fn: Callable[[], Any]) -> None:
        print(f"phase {name}", flush=True)
        started = time.monotonic()
        value = fn()
        elapsed = time.monotonic() - started
        if isinstance(value, list):
            phases[name] = {"elapsed_seconds": elapsed, "values": value}
        else:
            phases[name] = {"elapsed_seconds": elapsed, **value}

    if args.mode == "write":
        steps = [
            ("insert", lambda: insert(db, args.size_mib * 1024**2)),
            ("read", lambda: read(db)),
            ("journal-delete", lambda: journal_delete(db)),
            ("vacuum-rename", lambda: vacuum_rename(db)),
            ("rename-cycles", lambda: rename_cycles(db)),
            ("nested-dir-rename", lambda: nested_dir_rename(mount)),
            ("integrity-check", lambda: integrity(db)),
        ]
    else:
        steps = [
            ("verify-integrity-check", lambda: integrity(db)),
            ("verify-nested-integrity", lambda: nested_integrity(mount)),
            ("verify-wal-reopen", lambda: wal_reopen(db)),
        ]

    status = 0
    for name, fn in steps:
        started = time.monotonic()
        try:
            run(name, fn)
        except (sqlite3.Error, OSError, RuntimeError) as error:
            # Extended code like SQLITE_IOERR_FSYNC names the failing syscall
            code = getattr(error, "sqlite_errorname", None)
            result["error"] = (
                f"{name}: {type(error).__name__}: {error}"
                + (f" [{code}]" if code else "")
                + f" after {time.monotonic() - started:.1f}s"
            )
            print(result["error"], file=sys.stderr, flush=True)
            status = 1
            break

    Path(args.result).write_text(json.dumps(result, sort_keys=True))
    print(json.dumps(result, sort_keys=True), flush=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
