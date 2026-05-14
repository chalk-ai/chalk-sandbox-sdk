#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx", "python-dotenv"]
# ///

# # Shared SQLite on a Persistent Volume
#
# Chalk volumes let multiple containers share a filesystem backed by object
# storage. This example uses that to solve a common problem: one container
# writes structured data, and several others read it — without standing up a
# database server.
#
# The pattern works well for experiment tracking, lightweight feature stores,
# coordination state, or any case where you want persistence that outlives a
# single container but don't need a full RDBMS.
#
# ## Overview
#
# 1. Create a Chalk volume called `sqlite-persistence`.
# 2. Start a **writer** container, mount the volume at `/data`, and run a
#    Python script that creates a SQLite database with a `users` table.
# 3. Stop the writer — proving the data outlives the container.
# 4. Start two **reader** containers (`reader-a`, `reader-b`), each mounting
#    the same volume, and have them independently query the database.
# 5. Assert that both readers see identical rows.

import os
import sys
import time
import uuid

from dotenv import load_dotenv

# We import from the SDK at the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from chalkcompute import Container, Image  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))


# ## Configuration
#
# A single volume name and mount path shared by all three containers. The
# writer creates `/data/app.db`; each reader queries it.

VOLUME_NAME = f"sqlite-persistence-{uuid.uuid4().hex[:8]}"
MOUNT_PATH = "/data"
DB_PATH = f"{MOUNT_PATH}/app.db"


# ## The image
#
# All three containers share the same slim Python image. SQLite ships with
# Python's standard library, so no extra packages are needed.

image = Image.debian_slim("3.12")


# ## Helper: create the volume
#
# We use the Container's internal volume RPC to create the volume. In a
# production workflow you would use the `chalk volume create` CLI or the
# VolumeClient SDK directly.

def create_volume() -> None:
    """Create the shared volume (idempotent)."""
    c = Container._make_empty()
    c._ensure_auth()
    resp = c._volume_rpc("CreateVolume", {"name": VOLUME_NAME})
    if resp.status_code != 200 and "already exists" not in resp.text:
        raise RuntimeError(f"CreateVolume failed: {resp.text}")
    print(f"Volume '{VOLUME_NAME}' ready.")


def delete_volume() -> None:
    """Delete the shared volume."""
    c = Container._make_empty()
    c._ensure_auth()
    c._volume_rpc("DeleteVolume", {"name": VOLUME_NAME})
    print(f"Volume '{VOLUME_NAME}' deleted.")


def wait_for_file(container: Container, path: str, timeout: float = 120.0) -> None:
    """Poll until *path* exists inside the container."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if container.exec("test", "-e", path).exit_code == 0:
            return
        time.sleep(5)
    raise TimeoutError(f"{path} not visible after {timeout}s")


# ## Step 1 — Write the database
#
# The writer container runs an inline Python script that creates a SQLite
# database at `/data/app.db` and inserts three rows.

WRITE_SCRIPT = """
import sqlite3, os, sys

db_path = "{db_path}"
os.makedirs(os.path.dirname(db_path), exist_ok=True)

conn = sqlite3.connect(db_path)
conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)")
conn.execute("INSERT OR IGNORE INTO users VALUES (1, 'Alice', 'alice@example.com')")
conn.execute("INSERT OR IGNORE INTO users VALUES (2, 'Bob', 'bob@example.com')")
conn.execute("INSERT OR IGNORE INTO users VALUES (3, 'Charlie', 'charlie@example.com')")
conn.commit()

# Verify
rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
conn.close()
print(f"Wrote {{len(rows)}} rows to {{db_path}}")
for r in rows:
    print(f"  {{r}}")
""".format(db_path=DB_PATH)


# ## Step 2 — Read the database
#
# Each reader runs a one-liner that opens the same path in read-only mode
# and prints every row.

READ_SCRIPT = """
import sqlite3, sys

db_path = "{db_path}"
try:
    conn = sqlite3.connect(f"file:{{db_path}}?mode=ro", uri=True)
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    conn.close()
    for r in rows:
        print(r)
    print(f"Read {{len(rows)}} rows")
except Exception as e:
    print(f"ERROR: {{e}}", file=sys.stderr)
    sys.exit(1)
""".format(db_path=DB_PATH)


# ## Putting it all together

def main() -> None:
    create_volume()

    writer = None
    reader_a = None
    reader_b = None

    try:
        # --- Writer -----------------------------------------------------------
        # We start the writer, run the script, then stop it *before* starting
        # readers. This proves the data persists on the volume independently of
        # the container that wrote it.
        print("\n=== Starting writer container ===")
        writer = (
            Container(image=image)
            .mount_volume(VOLUME_NAME, MOUNT_PATH)
            .run()
        )

        wait_for_file(writer, MOUNT_PATH)
        result = writer.exec("python3", "-c", WRITE_SCRIPT, timeout_secs=30)
        print(result.stdout_text)
        if result.exit_code != 0:
            print(f"Writer failed: {result.stderr_text}", file=sys.stderr)
            sys.exit(1)

        writer.stop()
        writer = None
        print("Writer stopped.\n")

        # --- Readers ----------------------------------------------------------
        # Two independent containers mount the same volume and each query the
        # database. Both should see all three rows.
        print("=== Starting reader containers ===")
        reader_a = (
            Container(image=image)
            .mount_volume(VOLUME_NAME, MOUNT_PATH)
            .run()
        )
        reader_b = (
            Container(image=image)
            .mount_volume(VOLUME_NAME, MOUNT_PATH)
            .run()
        )

        for reader_name, reader in [("reader-a", reader_a), ("reader-b", reader_b)]:
            wait_for_file(reader, DB_PATH)

            print(f"\n--- {reader_name} output ---")
            r = reader.exec("python3", "-c", READ_SCRIPT, timeout_secs=30)
            print(r.stdout_text)
            if r.stderr_text:
                print(r.stderr_text, file=sys.stderr)
            assert r.exit_code == 0, f"{reader_name} failed"
            assert "3 rows" in r.stdout_text, f"{reader_name} didn't see all rows"

        print("\nBoth readers successfully queried the shared SQLite database.")

    finally:
        # --- Cleanup ----------------------------------------------------------
        for c in [writer, reader_a, reader_b]:
            if c is not None:
                try:
                    c.stop()
                except Exception:
                    pass
        delete_volume()


if __name__ == "__main__":
    main()
