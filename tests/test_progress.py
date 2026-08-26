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
