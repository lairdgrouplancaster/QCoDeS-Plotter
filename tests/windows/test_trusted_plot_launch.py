import sqlite3
import threading
import time

import numpy as np
import pytest
from PyQt6 import QtWidgets

from qplot.configuration.config import config
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.datahandling.test_trusted_plot import (
    make_run,
    prohibit_snapshots,
    protected_state,
)


def wait_for(predicate, timeout=20):
    app = QtWidgets.QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for trusted plot display")


@pytest.fixture
def auto_plot_window(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "default_path", str(tmp_path / "settings"))
    monkeypatch.setattr(config, "default_file", str(tmp_path / "settings" / config.config_file_name))
    prohibit_snapshots(monkeypatch)
    window = MainWindow()
    errors = []
    monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
    window.startupDatabaseTimer.stop()
    window.config.config["user_preference"]["confirm_close"] = False
    window.config.config["user_preference"]["confirm_close_all"] = False
    window.close_database(status=False)
    window.autoPlotBox.setChecked(False)
    yield window, errors
    close_main_window(window)


@pytest.mark.parametrize("dimensions", [1, 2])
def test_auto_plot_real_qcodes_run_waits_for_first_results(
        tmp_path, monkeypatch, auto_plot_window, dimensions):
    """A real Measurement creates its run before its first flushed result."""
    from qcodes.dataset import Measurement, load_or_create_experiment

    from qplot.datahandling import trusted_plot

    window, errors = auto_plot_window
    path = tmp_path / "delayed-data.db"
    seed_id, seed_guid, _ = make_run(path, dimensions=dimensions)
    writer = sqlite3.connect(path)
    writer.execute("SELECT count(*) FROM runs").fetchone()
    try:
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active and window.selected_run_id == seed_id)
        window.monitor.stop()
        window.autoPlotBox.setChecked(True)
        assert not window.windows  # The already-listed seed is complete.
        experiment = load_or_create_experiment("plots", sample_name="disposable")
        measurement = Measurement(exp=experiment)
        measurement.register_custom_parameter("x")
        if dimensions == 2:
            measurement.register_custom_parameter("y")
        measurement.register_custom_parameter(
            "z", setpoints=("x", "y") if dimensions == 2 else ("x",),
        )
        original = trusted_plot.plot_dataset
        metadata_threads = []
        def record_thread(*args):
            metadata_threads.append(threading.get_ident())
            return original(*args)
        monkeypatch.setattr(trusted_plot, "plot_dataset", record_thread)
        try:
            with measurement.run() as saver:
                guid = saver.dataset.guid
                before = protected_state(path)
                window.refreshMain()
                wait_for(lambda: len(window.windows) == 1 and not window.windows[0].worker.running)
                plot = window.windows[0]
                assert plot._dataset_key.guid == guid
                assert plot.monitor.isActive()
                assert protected_state(path) == before
                # Selection and another refresh do not own the automatic plot.
                window.updateSelected(seed_guid)
                window.refreshMain()
                wait_for(lambda: not window._database_refresh_active)
                values = [("x", 2), ("z", 7)]
                if dimensions == 2:
                    values.append(("y", 3))
                saver.add_result(*values)
                saver.flush_data_to_database(block=True)
            saver.dataset.conn.close()
        finally:
            experiment.conn.close()
        after_write = protected_state(path)
        wait_for(lambda: not plot.worker.running)
        plot.load_data()
        wait_for(lambda: not plot.worker.running)
        if dimensions == 1:
            np.testing.assert_array_equal(plot.axis_data["y"], [7])
        else:
            np.testing.assert_array_equal(plot.dataGrid, [[7]])
        assert len(window.windows) == 1
        assert metadata_threads and threading.get_ident() not in metadata_threads
        assert protected_state(path) == after_write
        assert not errors
    finally:
        writer.close()


@pytest.mark.parametrize("complete_when_ready", [False, True])
def test_auto_plot_delayed_description_and_completed_batch(
        tmp_path, monkeypatch, auto_plot_window, complete_when_ready):
    window, errors = auto_plot_window
    path = tmp_path / "delayed-description.db"
    seed_id, seed_guid, _ = make_run(path, dimensions=1)
    writer = sqlite3.connect(path)
    writer.execute("SELECT count(*) FROM runs").fetchone()
    try:
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active and window.selected_run_id == seed_id)
        window.monitor.stop()
        window.autoPlotBox.setChecked(True)
        late_id, late_guid, _ = make_run(path, dimensions=1)
        _, complete_guid, _ = make_run(path, dimensions=1)
        description = writer.execute("SELECT run_description FROM runs WHERE run_id=?", (late_id,)).fetchone()[0]
        # Model QCoDeS's separate run-creation and run-start commits.
        writer.execute("UPDATE runs SET run_description=NULL, is_completed=0, completed_timestamp=NULL WHERE run_id=?", (late_id,))
        writer.commit()
        before = protected_state(path)
        from qplot.windows import _auto_plot
        monkeypatch.setattr(_auto_plot, "RETRY_DELAYS", (0.05,))
        window.refreshMain()
        wait_for(lambda: getattr(window, "_auto_plot_queue", None) is not None and
                 late_guid in window._auto_plot_queue.runs and
                 window._auto_plot_queue.runs[late_guid].due == float("inf"))
        wait_for(lambda: len(window.windows) == 1 and not window.windows[0].worker.running)
        assert window.windows[0]._dataset_key.guid == complete_guid
        assert protected_state(path) == before
        assert not errors
        writer.execute("UPDATE runs SET run_description=?, is_completed=? WHERE run_id=?",
                       (description, complete_when_ready, late_id))
        writer.commit()
        after_write = protected_state(path)
        window.updateSelected(seed_guid)
        window.refreshMain()
        wait_for(lambda: len(window.windows) == 2 and all(not win.worker.running for win in window.windows))
        assert {win._dataset_key.guid for win in window.windows} == {late_guid, complete_guid}
        window.refreshMain()
        wait_for(lambda: not window._database_refresh_active)
        assert len(window.windows) == 2
        assert protected_state(path) == after_write
        assert not errors
    finally:
        writer.close()


@pytest.mark.parametrize("action", ["disable", "switch", "close", "shutdown"])
def test_auto_plot_pending_metadata_is_cancelled_by_lifecycle(
        tmp_path, monkeypatch, auto_plot_window, action):
    window, errors = auto_plot_window
    path = tmp_path / "pending.db"
    run_id, guid, _ = make_run(path, dimensions=1)
    other_path = tmp_path / "other.db"
    make_run(other_path, dimensions=1)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active and window.selected_run_id == run_id)
    window.monitor.stop()
    service = window._trusted_read_service
    submit = service.submit_plot_dataset
    held = []

    class HeldRequest:
        # Delay GUI publication of a real broker request, even if it completes.
        done = False
        cancelled = False

        def __init__(self, request):
            self.request = request

        def cancel(self):
            self.cancelled = True
            return self.request.cancel()

        def wait(self, timeout):
            return self.request.wait(timeout)

    def hold_first_request(*args, **kwargs):
        request = submit(*args, **kwargs)
        if not held:
            request = HeldRequest(request)
            held.append(request)
        return request

    monkeypatch.setattr(service, "submit_plot_dataset", hold_first_request)
    window.autoPlotBox.setChecked(True)
    from qplot.windows._auto_plot import enqueue_auto_plot
    enqueue_auto_plot(window, [guid])
    wait_for(lambda: bool(held))
    if action == "disable":
        # A manual request for the same run remains independently owned.
        window.openPlot(guid)
        window.autoPlotBox.setChecked(False)
        wait_for(lambda: len(window.windows) == 1 and not window.windows[0].worker.running)
    elif action == "switch":
        assert window.load_file(str(other_path))
        wait_for(lambda: not window._database_load_active)
    elif action == "close":
        window.close_database(status=False)
    else:
        window._cancel_plot_work()
    assert held[0].cancelled
    assert window._auto_plot_queue is None
    held[0].done = True
    QtWidgets.QApplication.instance().processEvents()
    assert len(window.windows) == (1 if action == "disable" else 0)
    assert not errors


def test_auto_plot_toggle_uses_newest_incomplete_run_and_manual_plot_still_works(
        tmp_path, auto_plot_window):
    window, errors = auto_plot_window
    path = tmp_path / "toggle.db"
    _, old_guid, _ = make_run(path, dimensions=1)
    new_id, new_guid, _ = make_run(path, dimensions=1)
    writer = sqlite3.connect(path)
    try:
        writer.execute("UPDATE runs SET is_completed=0, completed_timestamp=NULL")
        writer.commit()
        before = protected_state(path)
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active and window.selected_run_id == new_id)
        window.monitor.stop()
        assert not window.windows
        window.autoPlotBox.setChecked(True)
        wait_for(lambda: len(window.windows) == 1 and not window.windows[0].worker.running)
        assert window.windows[0]._dataset_key.guid == new_guid
        assert protected_state(path) == before
        window.autoPlotBox.setChecked(False)
        assert window._auto_plot_queue is None
        _, disabled_guid, _ = make_run(path, dimensions=1)
        after_write = protected_state(path)
        window.refreshMain()
        wait_for(lambda: not window._database_refresh_active)
        assert len(window.windows) == 1
        window.openPlot(disabled_guid)
        window.openPlot(old_guid)
        wait_for(lambda: len(window.windows) == 3 and all(not win.worker.running for win in window.windows))
        assert protected_state(path) == after_write
        assert not errors
    finally:
        writer.close()


@pytest.mark.parametrize("action", ["all", "double_click", "run", "preview", "run_preview", "index", "live", "switch", "aggregate"])
def test_trusted_launch_and_refresh_never_copy_database(tmp_path, monkeypatch, action):
    path = tmp_path / "source.db"
    run_id, guid, table = make_run(path)
    writer = sqlite3.connect(path)
    if action in ("live", "switch", "aggregate"):
        writer.execute("UPDATE runs SET is_completed=0 WHERE run_id=?", (run_id,))
        writer.commit()
    monkeypatch.setattr(config, "default_path", str(tmp_path / "settings"))
    monkeypatch.setattr(config, "default_file", str(tmp_path / "settings" / config.config_file_name))
    prohibit_snapshots(monkeypatch)
    from qplot.datahandling import trusted_plot
    gui_thread = threading.get_ident()
    original_metadata, original_prefix = trusted_plot.plot_dataset, trusted_plot.plot_prefix
    def metadata_off_gui(*args):
        assert threading.get_ident() != gui_thread
        return original_metadata(*args)
    def prefix_off_gui(*args):
        assert threading.get_ident() != gui_thread
        return original_prefix(*args)
    monkeypatch.setattr(trusted_plot, "plot_dataset", metadata_off_gui)
    monkeypatch.setattr(trusted_plot, "plot_prefix", prefix_off_gui)
    window = MainWindow()
    errors = []
    monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
    try:
        window.startupDatabaseTimer.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        if action == "aggregate":
            window.config.config["runtime_settings"]["max_full_heatmap_points"] = 4
            monkeypatch.setattr(trusted_plot, "PlotPrefix", lambda: pytest.fail("staged raw rows"))
        window.close_database(status=False)
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active and window.selected_run_id == run_id)
        window.monitor.stop()
        assert window._database_access_mode == "trusted_live"
        if action in ("all", "live", "switch", "aggregate"):
            window.openPlot(guid, show=True)
        elif action == "double_click":
            item = window.RunList.currentItem()
            assert item.guid == guid
            window.RunList.itemDoubleClicked.emit(item, 0)
        elif action == "run":
            window.measurementBox.setText("*")
            window.openRun()
        elif action == "preview":
            window.open_preview_plot("z")
        elif action == "run_preview":
            window.open_run_preview_plot(guid, "z")
        else:
            window.open_param_by_index(0)
        wait_for(lambda: errors or (window.windows and not window.windows[-1].worker.running))
        assert not errors
        plot = window.windows[-1]
        assert plot.isVisible()
        assert plot.plot_state_overlay.frame.isHidden()
        assert not plot.plot_state_overlay.progress_timer.isActive()
        np.testing.assert_array_equal(plot.dataGrid, np.arange(3)[:, None] * 10 + np.arange(4))
        if action != "aggregate":
            assert plot.worker.heatmap_full_resolution
            assert plot.heatmap_resolution_label.text() == "Resolution: full 4 x 3"
        else:
            assert not plot.worker.heatmap_full_resolution
            assert "downsampled" in plot.heatmap_resolution_label.text()
        if action in ("live", "switch", "aggregate"):
            if action == "switch":
                second = tmp_path / "second.db"
                make_run(second)
                retained_service = plot.ds.service
                assert window.load_file(str(second))
                wait_for(lambda: not window._database_load_active)
                assert retained_service is not window._trusted_read_service
                assert not retained_service.closing and not retained_service.closed
            writer.executemany(f'INSERT INTO "{table}" (x,y,z) VALUES (?,?,?)',
                               [(3, y, 30 + y) for y in range(4)])
            writer.execute("UPDATE runs SET is_completed=1 WHERE run_id=?", (run_id,))
            writer.commit()
            plot.refreshWindow()
            wait_for(lambda: plot.dataGrid.shape == (4, 4) and not plot.worker.running)
            np.testing.assert_array_equal(plot.dataGrid, np.arange(4)[:, None] * 10 + np.arange(4))
            assert plot.ds.completed
            assert not plot.plot_state_overlay.progress_timer.isActive()
            assert not plot._refresh_monitor_required()
            return
        assert plot.load_data()
        wait_for(lambda: not plot.worker.running)
        assert not errors
        np.testing.assert_array_equal(plot.dataGrid, np.arange(3)[:, None] * 10 + np.arange(4))
    finally:
        close_main_window(window)
        writer.close()


@pytest.mark.parametrize("arrays", [False, True, "aggregate"])
@pytest.mark.parametrize("live", [False, True])
def test_small_plot_and_refresh_progress_during_another_capture(tmp_path, monkeypatch, arrays, live):
    """Even a one-thread plot pool must not queue behind broker capture waits."""
    from qplot.datahandling import trusted_plot
    from tests.datahandling.test_trusted_plot import protected_state

    path = tmp_path / "parallel.db"
    large_id, large_guid, _ = make_run(path, arrays=arrays is True)
    small_id, small_guid, table = make_run(path)
    writer = sqlite3.connect(path)
    writer.execute("UPDATE runs SET is_completed=? WHERE run_id=?", (not live, small_id))
    writer.commit()
    before = protected_state(path)
    prohibit_snapshots(monkeypatch)
    monkeypatch.setattr(config, "default_path", str(tmp_path / "settings"))
    monkeypatch.setattr(config, "default_file", str(tmp_path / "settings" / config.config_file_name))
    monkeypatch.setattr(trusted_plot, "PAGE_ROWS", 2)
    if arrays == "aggregate":
        from dataclasses import replace

        from qplot.tools.worker import loader
        original_plan = loader._trusted_heatmap_plan
        def large_plan(self):
            plan = original_plan(self)
            return replace(plan, full_limit=4) if self.cache._dataset.run_id == large_id else plan
        monkeypatch.setattr(loader, "_trusted_heatmap_plan", large_plan)
    reached, release = threading.Event(), threading.Event()
    original = trusted_plot.plot_prefix
    private_paths = []
    closed = threading.Event()
    original_init = trusted_plot.PlotPrefix.__init__

    def record_spool(self):
        original_init(self)
        private_paths.append(self.path)

    def slow_capture(executor, dataset, *options):
        steps = original(executor, dataset, *options)
        try:
            if dataset.run_id == large_id:
                # A numeric preflight precedes its first page.
                if arrays == "aggregate":
                    next(steps)
                next(steps)  # A real page or array chunk; no source lock remains.
                reached.set()
                while not release.is_set():
                    executor.check_cancelled()
                    time.sleep(0.002)
                    yield
            return (yield from steps)
        finally:
            steps.close()
            if dataset.run_id == large_id:
                closed.set()

    monkeypatch.setattr(trusted_plot.PlotPrefix, "__init__", record_spool)
    monkeypatch.setattr(trusted_plot, "plot_prefix", slow_capture)
    window = MainWindow()
    errors = []
    monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
    try:
        window.startupDatabaseTimer.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        window.threadPool.setMaxThreadCount(1)
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active)
        window.monitor.stop()
        def double_click(guid):
            item = next(window.RunList.topLevelItem(index)
                        for index in range(window.RunList.topLevelItemCount())
                        if window.RunList.topLevelItem(index).guid == guid)
            window.RunList.setCurrentItem(item)
            window.RunList.itemDoubleClicked.emit(item, 0)
        double_click(large_guid)
        wait_for(reached.is_set)
        large = window.windows[-1]
        large.monitor.stop()
        assert large.worker.running
        assert window.threadPool.activeThreadCount() == 0
        double_click(small_guid)
        wait_for(lambda: len(window.windows) == 2 and
                 not window.windows[-1].worker.running)
        small = window.windows[-1]
        small.monitor.stop()
        np.testing.assert_array_equal(small.dataGrid, np.arange(3)[:, None] * 10 + np.arange(4))
        assert large.worker.running
        assert protected_state(path) == before
        # A completed reload or concurrent acquisition also makes progress.
        if live:
            assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
            writer.executemany(f'INSERT INTO "{table}" (x,y,z) VALUES (?,?,?)',
                               [(3, y, 30 + y) for y in range(4)])
            writer.execute("UPDATE runs SET is_completed=1 WHERE run_id=?", (small_id,))
            writer.commit()
        after_append = protected_state(path)
        assert small.load_data()
        wait_for(lambda: not small.worker.running)
        np.testing.assert_array_equal(small.dataGrid, np.arange(4 if live else 3)[:, None] * 10 + np.arange(4))
        assert large.worker.running
        # Cancel while the large spool is suspended (including an open private BLOB).
        large.worker.cancel()
        wait_for(lambda: not large.worker.running)
        wait_for(closed.is_set)
        if arrays != "aggregate":
            wait_for(lambda: not private_paths[0].exists())
        assert protected_state(path) == after_append
        assert not errors
    finally:
        release.set()
        close_main_window(window)
        writer.close()
