"""Accepted Close All also retires plot requests awaiting GUI publication."""

import hashlib
import sys
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect

from qplot.diagnostics import configure_logging
from qplot.windows._auto_plot import enqueue_auto_plot
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


def _protected_artifacts(database):
    result = {}
    for suffix in ("", "-wal", "-journal"):
        path = Path(f"{database}{suffix}")
        if not path.exists():
            result[suffix] = None
            continue
        metadata = path.stat()
        result[suffix] = (
            metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return result


def _record_run(writer, experiment, database, offset):
    assert Path(writer.execute("PRAGMA database_list").fetchone()[2]).resolve() == database
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("bias")
    measurement.register_custom_parameter("signal", setpoints=("bias",))
    with measurement.run(write_in_background=False) as saver:
        assert Path(
            saver.dataset.conn.execute("PRAGMA database_list").fetchone()[2]
        ).resolve() == database
        for bias in (-4., -1., 0., 3., 8.):
            saver.add_result(("bias", bias), ("signal", offset + bias * bias))
    return saver.dataset.guid


@pytest.fixture
def pending_plot_window(tmp_path, monkeypatch, qapplication):
    configure_temp_qplot(monkeypatch, tmp_path)
    configure_logging(tmp_path / "qplot.log", force=True)
    database = (tmp_path / "pending-close.db").resolve()
    initialise_or_create_database_at(str(database), journal_mode="WAL")
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    with closing(connect(str(database))) as writer:
        experiment = load_or_create_experiment("pending close", "round 33", conn=writer)
        guids = [_record_run(writer, experiment, database, offset) for offset in (11., 31., 101.)]
        original = _protected_artifacts(database)
        window = MainWindow()
        monkeypatch.setattr(window, "show_error", lambda *error: errors.append(error))
        service = None
        try:
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            window.config.config["user_preference"]["confirm_close"] = False
            window.config.config["user_preference"]["confirm_close_all"] = True
            window.config.config["runtime_settings"]["del_grace_period"] = 0
            assert window.load_file(str(database))
            wait_for(lambda: not window._database_load_active and not window._database_detail_active)
            service = window._trusted_read_service
            assert window._selected_run_guid == guids[-1]
            yield window, guids, writer, experiment, database
        finally:
            # Capture before close_main_window deletes the underlying QObject.
            baseline = getattr(window, "_test_protected_baseline", original)
            close_main_window(window)
            assert service is None or service.closed
            assert _protected_artifacts(database) == baseline
            assert not errors


def _open_original(window, guid):
    window.openPlot(guid=guid)
    wait_for(lambda: len(window.windows) == 1
             and hasattr(window.windows[0], "axis_data")
             and not window.windows[0].worker.running)
    plot = window.windows[0]
    plot.monitor.stop()
    np.testing.assert_array_equal(plot.axis_data["y"], [117., 102., 101., 110., 165.])
    return plot


def _drain_plot_publication(window):
    wait_for(lambda: not getattr(window, "_trusted_plot_pending", ())
             and not getattr(getattr(window, "_auto_plot_queue", None), "runs", {})
             and all(hasattr(plot, "axis_data") and not plot.worker.running
                     for plot in window.windows), timeout=30)
    # Let retired timer callbacks run even when cancellation already removed
    # their requests from the pending collection.
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(60, loop.quit)
    loop.exec()


def _queue_plot(window, guid, automatic):
    if automatic:
        window.autoPlotBox.setChecked(True)
        enqueue_auto_plot(window, [guid])
        queue = window._auto_plot_queue
        if queue.active is None:
            queue.poll()
    else:
        window.openPlot(guid=guid)


def _pending_count(window, automatic):
    if automatic:
        return len(window._auto_plot_queue.runs)
    return len(window._trusted_plot_pending)


@pytest.mark.parametrize("accept", [False, True], ids=["cancel", "accept"])
@pytest.mark.parametrize("automatic", [False, True], ids=["manual", "automatic"])
def test_close_all_confirmation_retires_only_accepted_pending_launches(
    pending_plot_window, qapplication, accept, automatic,
):
    window, guids, _writer, _experiment, _database = pending_plot_window
    original = _open_original(window, guids[-1])
    _queue_plot(window, guids[0], automatic)
    assert _pending_count(window, automatic) == 1
    assert window.windows == [original]
    observations = []
    timeouts = []
    timeout = QtCore.QTimer()
    timeout.setSingleShot(True)

    def answer():
        dialog = qapplication.activeModalWidget()
        observations.append((isinstance(dialog, qtw.QMessageBox),
                             _pending_count(window, automatic), len(window.windows)))
        if isinstance(dialog, qtw.QMessageBox):
            dialog.button(qtw.QMessageBox.StandardButton.Yes if accept
                          else qtw.QMessageBox.StandardButton.No).click()

    def abort_confirmation():
        timeouts.append(True)
        dialog = qapplication.activeModalWidget()
        if dialog is not None:
            dialog.reject()

    timeout.timeout.connect(abort_confirmation)
    timeout.start(10_000)
    QtCore.QTimer.singleShot(0, answer)
    try:
        window.closeAll()
    finally:
        timeout.stop()
    assert not timeouts
    assert observations == [(True, 1, 1)]
    _drain_plot_publication(window)
    if accept:
        assert window.windows == []
        assert original._closed
    else:
        assert window.windows[0] is original
        assert len(window.windows) == 2
        assert not original._closed
        late = window.windows[1]
        assert late.ds.guid == guids[0]
        np.testing.assert_array_equal(late.axis_data["y"], [27., 12., 11., 20., 75.])


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("automatic", [False, True], ids=["manual", "automatic"])
def test_close_all_without_visible_plots_retires_pending_launches(
    pending_plot_window, confirm, automatic,
):
    window, guids, _writer, _experiment, _database = pending_plot_window
    window.config.config["user_preference"]["confirm_close_all"] = confirm
    _queue_plot(window, guids[0], automatic)
    _queue_plot(window, guids[1], automatic)
    assert _pending_count(window, automatic) == 2
    assert window.windows == []
    window.closeAll()
    _drain_plot_publication(window)
    assert window.windows == []


@pytest.mark.parametrize("automatic", [False, True], ids=["manual", "automatic"])
def test_unconfirmed_close_all_retires_pending_launches(pending_plot_window, automatic):
    window, guids, _writer, _experiment, _database = pending_plot_window
    original = _open_original(window, guids[-1])
    window.config.config["user_preference"]["confirm_close_all"] = False
    _queue_plot(window, guids[0], automatic)
    assert _pending_count(window, automatic) == 1
    window.closeAll()
    _drain_plot_publication(window)
    assert window.windows == []
    assert original._closed


@pytest.mark.parametrize("same_guid", [False, True])
def test_new_plot_request_survives_retirement_of_older_pending_requests(
    pending_plot_window, same_guid,
):
    window, guids, _writer, _experiment, _database = pending_plot_window
    window.openPlot(guid=guids[0])
    window.openPlot(guid=guids[0])
    assert len(window._trusted_plot_pending) == 2
    window.closeAll()
    # Submit before the old timer callbacks drain. Their cleanup must not
    # discard or cancel a newer request, including the exact same dataset key.
    requested = guids[0] if same_guid else guids[1]
    window.openPlot(guid=requested)
    _drain_plot_publication(window)
    assert len(window.windows) == 1
    assert window.windows[0].ds.guid == requested
    expected = [27., 12., 11., 20., 75.] if same_guid else [47., 32., 31., 40., 95.]
    np.testing.assert_array_equal(window.windows[0].axis_data["y"], expected)


@pytest.mark.parametrize("automatic", [False, True], ids=["manual", "automatic"])
def test_close_all_preserves_auto_plot_for_later_acquisitions(pending_plot_window, automatic):
    window, guids, writer, experiment, database = pending_plot_window
    window.autoPlotBox.setChecked(True)
    _queue_plot(window, guids[0], automatic)
    assert _pending_count(window, automatic) == 1
    window.closeAll()
    _drain_plot_publication(window)
    assert window.windows == []
    later = _record_run(writer, experiment, database, 503.)
    window._test_protected_baseline = _protected_artifacts(database)
    window.refreshMain()
    wait_for(lambda: len(window.windows) == 1
             and hasattr(window.windows[0], "axis_data")
             and not window.windows[0].worker.running, timeout=30)
    plot = window.windows[0]
    assert plot.ds.guid == later
    assert window.autoPlotBox.isChecked()
    np.testing.assert_array_equal(plot.axis_data["y"], [519., 504., 503., 512., 567.])
    if automatic:
        # Metadata replay must not reopen a GUID retired by Close All.
        enqueue_auto_plot(window, [guids[0], later])
        assert not window._auto_plot_queue.runs
    window.refreshMain()
    wait_for(lambda: not window._database_refresh_active)
    assert window.windows == [plot]
