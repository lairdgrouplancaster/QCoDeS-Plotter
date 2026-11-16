from types import SimpleNamespace

import pytest

from qplot.windows._database_actions import DatabaseActionsMixin
from qplot.windows._database_detail_state import detail_channel
from qplot.windows._shutdown_coordinator import ShutdownCoordinator
from qplot.windows._widgets.treeWidgets import RunList
from tests._database_load_fakes import seed_load


def test_shutdown_invalidates_both_channels_before_late_callbacks():
    window = SimpleNamespace(
        _database_load_generation=1,
        show_status=lambda *_args: (_ for _ in ()).throw(AssertionError("late status")),
    )
    seed_load(window, generation=1)
    basic = detail_channel(window)
    expensive = detail_channel(window, True)
    basic.generation, expensive.generation = 3, 7
    basic.active = expensive.active = True
    basic.worker = expensive.worker = object()

    ShutdownCoordinator._invalidate_database_publications(SimpleNamespace(window=window))

    assert (basic.generation, expensive.generation) == (4, 8)
    assert not basic.active and not expensive.active
    assert basic.worker is None and expensive.worker is None
    DatabaseActionsMixin.database_detail_status(window, 3, "late basic")
    DatabaseActionsMixin.database_expensive_detail_status(window, 7, "late expensive")


@pytest.mark.parametrize("error", [None, RuntimeError("Metadata unavailable")])
def test_metadata_loading_tracks_independent_snapshot_stages(tmp_path, monkeypatch, error):
    run_list = RunList()
    run_list.addRuns({1: {"guid": "pending"}})
    item = run_list._item_for_guid("pending")
    path = str(tmp_path / "snapshot.db")
    workers = []
    window = SimpleNamespace(
        RunList=run_list,
        fileTextbox=SimpleNamespace(text=lambda: path),
        databaseLoadThreadPool=SimpleNamespace(start=workers.append),
        _database_detail_priority_run_ids=lambda: [1],
        _apply_database_detail_batch=run_list.updateRuns,
        show_status=lambda *_args: None,
    )
    for prefix in ("database_detail", "database_expensive_detail"):
        for name in ("status", "batch_ready", "finished"):
            setattr(window, f"{prefix}_{name}", lambda *_args: None)
    monkeypatch.setattr(DatabaseActionsMixin, "_reload_if_worker_database_instance_changed",
                        lambda *_args, **_kwargs: False)

    def text(column):
        return item.text(run_list.cols.index(column))

    try:
        for expensive in (False, True):
            DatabaseActionsMixin._start_database_detail_worker(
                window, path, [1], None, {}, expensive=expensive,
            )
        assert len(workers) == 2
        assert all(text(name) == "Loading…" for name in ("Status", "Setpoints", "Size"))

        DatabaseActionsMixin._detail_batch_ready(
            window, 1, path, {1: {"guid": "pending", "is_completed": True}}, expensive=False,
        )
        assert text("Status") == "Completed"
        assert text("Duration") == "Unknown"
        assert text("Setpoints") == text("Size") == "Loading…"
        DatabaseActionsMixin._detail_finished(window, 1, path, None, expensive=False)
        assert text("Size") == "Loading…"

        DatabaseActionsMixin._detail_batch_ready(
            window, 1, path, {1: {"guid": "pending", "setpoint_count": 12}}, expensive=True,
        )
        assert text("Setpoints") == "12"
        assert text("Size") == "Loading…"
        DatabaseActionsMixin._detail_finished(window, 1, path, error, expensive=True)
        assert text("Size") == "Unknown"
        assert not item.loading_columns

        DatabaseActionsMixin._start_database_detail_worker(
            window, path, [1], None, {}, expensive=True,
        )
        DatabaseActionsMixin._detail_finished(window, 1, path, error, expensive=True)
        assert text("Size") == "Loading…"  # Old completion cannot clear a newer load.
        DatabaseActionsMixin._cancel_database_detail_load(window)
        assert text("Size") == "Unknown"
        assert not item.loading_columns
    finally:
        DatabaseActionsMixin._cancel_database_detail_load(window)
        run_list.deleteLater()


@pytest.mark.parametrize("expensive", [False, True])
@pytest.mark.parametrize("failure", ["construction", "submission"])
def test_snapshot_startup_failure_clears_real_run_list_and_allows_retry(monkeypatch, expensive, failure):
    from qplot.windows import _database_detail_ui as detail_ui

    run_list = RunList()
    run_list.addRuns({1: {"guid": "pending"}})
    item = run_list._item_for_guid("pending")
    window = SimpleNamespace(
        RunList=run_list,
        fileTextbox=SimpleNamespace(text=lambda: "selected.db"),
        databaseLoadThreadPool=SimpleNamespace(start=lambda worker: None),
        _database_detail_priority_run_ids=lambda: [1],
        show_status=lambda *_args: None,
    )
    for prefix in ("database_detail", "database_expensive_detail"):
        for name in ("status", "batch_ready", "finished"):
            setattr(window, f"{prefix}_{name}", lambda *_args: None)

    def start(stage):
        DatabaseActionsMixin._start_database_detail_worker(window, "selected.db", [1], None, {}, expensive=stage)

    def fail(*_args, **_kwargs):
        raise RuntimeError("Startup failed")

    try:
        start(not expensive)
        with monkeypatch.context() as patch:
            if failure == "construction":
                patch.setattr(detail_ui, "DatabaseExpensiveDetailWorker" if expensive else "DatabaseDetailWorker", fail)
            else:
                patch.setattr(window.databaseLoadThreadPool, "start", fail)
            start(expensive)
        assert not detail_channel(window, expensive).active
        assert detail_channel(window, expensive).worker is None
        assert detail_channel(window, not expensive).active
        failed_column, other_column = ("Size", "Status") if expensive else ("Status", "Size")
        assert item.text(run_list.cols.index(failed_column)) == "Unknown"
        assert item.text(run_list.cols.index(other_column)) == "Loading…"
        start(expensive)
        assert item.text(run_list.cols.index(failed_column)) == "Loading…"
        assert detail_channel(window, expensive).active
        DatabaseActionsMixin._cancel_database_detail_load(window)
        assert not item.loading_columns
    finally:
        DatabaseActionsMixin._cancel_database_detail_load(window)
        run_list.deleteLater()
