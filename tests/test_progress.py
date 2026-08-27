from __future__ import annotations

from chalkcompute._progress import DeployProgress


def test_progress_stays_live_until_all_concurrent_resources_finish(monkeypatch) -> None:
    progress = DeployProgress()
    stops: list[None] = []

    def start() -> None:
        progress._started = True

    monkeypatch.setattr(progress, "_ensure_live", start)
    monkeypatch.setattr(progress, "_refresh", lambda: None)
    monkeypatch.setattr(progress, "_stop_live", lambda: stops.append(None))

    progress.add("first", "function", "First")
    progress.add("second", "function", "Second")
    progress.update("first", status="done")
    progress.finish_if_idle()

    assert stops == []

    progress.update("second", status="done")
    progress.finish_if_idle()

    assert stops == [None]


def test_completed_progress_tracker_is_reused_without_old_rows(monkeypatch) -> None:
    progress = DeployProgress()

    def start() -> None:
        progress._started = True

    def stop() -> None:
        progress._started = False

    monkeypatch.setattr(progress, "_ensure_live", start)
    monkeypatch.setattr(progress, "_refresh", lambda: None)
    monkeypatch.setattr(progress, "_stop_live", stop)

    progress.add("first", "function", "First")
    progress.update("first", status="done")
    progress.finish_if_idle()
    progress.add("second", "function", "Second")

    assert progress._order == ["second"]
    assert set(progress._resources) == {"second"}


def test_failed_root_can_finish_with_an_active_child(monkeypatch) -> None:
    progress = DeployProgress()
    stops: list[None] = []

    monkeypatch.setattr(progress, "_ensure_live", lambda: None)
    monkeypatch.setattr(progress, "_refresh", lambda: None)
    monkeypatch.setattr(progress, "_stop_live", lambda: stops.append(None))

    progress.add("function", "function", "Function")
    progress.add("image", "image", "Image", parent="function")
    progress.update("function", status="failed")
    progress.finish_if_idle()

    assert stops == [None]


def test_reuse_clear_keeps_rows_of_a_still_running_operation(monkeypatch) -> None:
    """A finished operation must not erase a concurrent operation's subtree."""
    progress = DeployProgress()

    def start() -> None:
        progress._started = True

    def stop() -> None:
        progress._started = False

    monkeypatch.setattr(progress, "_ensure_live", start)
    monkeypatch.setattr(progress, "_refresh", lambda: None)
    monkeypatch.setattr(progress, "_stop_live", stop)

    # Operation A registers a root plus a child, then stalls mid-flight.
    progress.add("fn:a", "function", "A")
    progress.add("build:a", "image", "Building A", parent="fn:a")

    # Operation B runs to completion and stops the display.
    progress.add("fn:b", "function", "B")
    progress.update("fn:b", status="done")
    progress._started = False

    # A third operation reuses the tracker; B's rows go, A's subtree stays.
    progress.add("fn:c", "function", "C")

    assert set(progress._resources) == {"fn:a", "build:a", "fn:c"}
    assert progress._order == ["fn:a", "fn:c"]

    # A can still finish rendering its own rows.
    progress.update("build:a", status="done")
    assert progress._resources["build:a"].status == "done"


def test_terminal_root_skips_its_unfinished_children(monkeypatch) -> None:
    """A finished operation has no running steps, so the display can idle."""
    progress = DeployProgress()
    stops: list[None] = []

    def start() -> None:
        progress._started = True

    def stop() -> None:
        progress._started = False
        stops.append(None)

    monkeypatch.setattr(progress, "_ensure_live", start)
    monkeypatch.setattr(progress, "_refresh", lambda: None)
    monkeypatch.setattr(progress, "_stop_live", stop)

    progress.add("fn:a", "function", "A")
    progress.add("build:a", "image", "Building A", parent="fn:a")
    progress.update("fn:a", status="failed")

    # The child cannot still be running once its operation failed.
    assert progress._resources["build:a"].status == "skipped"

    # Both terminality checks now agree, so the display idles...
    progress.finish_if_idle()
    assert stops == [None]

    # ...and the whole subtree is dropped when the tracker is reused.
    progress.add("fn:b", "function", "B")
    assert set(progress._resources) == {"fn:b"}
