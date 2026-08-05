"""End-to-end test: shared SQLite database on a Chalk volume.

Proves that a SQLite database written by one container is readable by two
independent containers that mount the same volume.
"""

import time
import uuid

import pytest

from chalkcompute import Container, Image, Volume
from e2etests.conftest import needs_staging

pytestmark = pytest.mark.skip(
    reason="expensive — overlaps with test_volume for the volume+container path"
)

MOUNT_PATH = "/data"
DB_PATH = f"{MOUNT_PATH}/app.db"

WRITE_SCRIPT = """\
import sqlite3, os
db = "{db_path}"
os.makedirs(os.path.dirname(db), exist_ok=True)
conn = sqlite3.connect(db)
conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)")
conn.execute("INSERT OR IGNORE INTO users VALUES (1, 'Alice', 'alice@example.com')")
conn.execute("INSERT OR IGNORE INTO users VALUES (2, 'Bob', 'bob@example.com')")
conn.execute("INSERT OR IGNORE INTO users VALUES (3, 'Charlie', 'charlie@example.com')")
conn.commit()
rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
conn.close()
print(f"wrote {{len(rows)}} rows")
for r in rows:
    print(r)
""".format(db_path=DB_PATH)

READ_SCRIPT = """\
import sqlite3, sys
db = "{db_path}"
conn = sqlite3.connect(f"file:{{db}}?mode=ro", uri=True)
rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
conn.close()
for r in rows:
    print(r)
print(f"read {{len(rows)}} rows")
""".format(db_path=DB_PATH)

image = Image.debian_slim()


def _create_volume(name: str) -> None:
    Volume(name).reload()


def _delete_volume(name: str) -> None:
    Volume(name, create_if_missing=False).delete()


@pytest.fixture()
def volume_name() -> str:
    name = f"test-sqlite-{uuid.uuid4().hex[:8]}"
    _create_volume(name)
    yield name
    _delete_volume(name)


def _wait_for_path(container: Container, path: str, timeout: float = 120.0) -> None:
    """Poll until *path* exists inside the container (file or directory)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if container.exec("test", "-e", path).exit_code == 0:
            return
        time.sleep(5)
    raise TimeoutError(f"{path} not visible after {timeout}s")


def _wait_for_writable_mount(
    container: Container, path: str, timeout: float = 120.0
) -> None:
    """Poll until *path* is a writable directory (ChalkFS FUSE mount ready)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = container.exec("sh", "-c", f"touch {path}/.probe && rm -f {path}/.probe")
        if r.exit_code == 0:
            return
        time.sleep(5)
    raise TimeoutError(f"{path} not writable after {timeout}s")


@needs_staging
def test_writer_creates_database(volume_name: str) -> None:
    """The writer container creates a SQLite DB on the volume."""
    writer = Container(image=image).mount_volume(volume_name, MOUNT_PATH).run()
    try:
        _wait_for_writable_mount(writer, MOUNT_PATH)
        result = writer.exec("python3", "-c", WRITE_SCRIPT, timeout_secs=30)
        assert result.exit_code == 0, f"Writer failed: {result.stderr_text}"
        assert "wrote 3 rows" in result.stdout_text
    finally:
        writer.stop()


@needs_staging
def test_two_readers_see_same_data(volume_name: str) -> None:
    """Two reader containers independently query the same SQLite DB."""
    # --- Write phase ---
    writer = Container(image=image).mount_volume(volume_name, MOUNT_PATH).run()
    try:
        _wait_for_writable_mount(writer, MOUNT_PATH)
        result = writer.exec("python3", "-c", WRITE_SCRIPT, timeout_secs=30)
        assert result.exit_code == 0, f"Writer failed: {result.stderr_text}"
    finally:
        writer.stop()

    # --- Read phase (two independent containers) ---
    reader_a = Container(image=image).mount_volume(volume_name, MOUNT_PATH).run()
    reader_b = Container(image=image).mount_volume(volume_name, MOUNT_PATH).run()
    try:
        _wait_for_path(reader_a, DB_PATH)
        _wait_for_path(reader_b, DB_PATH)

        ra = reader_a.exec("python3", "-c", READ_SCRIPT, timeout_secs=30)
        rb = reader_b.exec("python3", "-c", READ_SCRIPT, timeout_secs=30)

        assert ra.exit_code == 0, f"reader-a failed: {ra.stderr_text}"
        assert rb.exit_code == 0, f"reader-b failed: {rb.stderr_text}"

        assert "read 3 rows" in ra.stdout_text, f"reader-a output: {ra.stdout_text}"
        assert "read 3 rows" in rb.stdout_text, f"reader-b output: {rb.stdout_text}"

        # Both readers should see identical output
        assert ra.stdout_text == rb.stdout_text, (
            f"Readers disagree:\n  reader-a: {ra.stdout_text}\n  reader-b: {rb.stdout_text}"
        )
    finally:
        reader_a.stop()
        reader_b.stop()
