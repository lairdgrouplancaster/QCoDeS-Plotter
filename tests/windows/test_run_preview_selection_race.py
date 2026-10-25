"""Explicit thumbnail launches must not replace a later run selection."""

import csv
import hashlib
import sys
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from PyQt6 import QtCore, QtGui, QtTest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect

from qplot.diagnostics import configure_logging
from qplot.windows._export_paths import prepare_export_destination
from qplot.windows._widgets.preview import PreviewImageLabel
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
        info = path.stat()
        result[suffix] = (
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return result


@pytest.fixture
def preview_window(tmp_path, monkeypatch, qapplication):
    configure_temp_qplot(monkeypatch, tmp_path)
    configure_logging(tmp_path / "qplot.log", force=True)
    database = tmp_path / "preview-selection.db"
    initialise_or_create_database_at(str(database), journal_mode="WAL")
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    with closing(connect(str(database))) as writer:
        experiment = load_or_create_experiment("preview selection", "race", conn=writer)
        guids = []
        for offset in (50., 500.):
            measurement = Measurement(exp=experiment)
            measurement.register_custom_parameter("drive")
            measurement.register_custom_parameter("response", setpoints=("drive",))
            with measurement.run(write_in_background=False) as saver:
                for drive in (-3., -1., 2., 5., 8.):
                    saver.add_result(("drive", drive), ("response", offset + drive * drive))
            guids.append(saver.dataset.guid)
        # Only the fixture writer checkpoints, before qPlot views the source.
        # CSV actions can then use their ordinary one-shot snapshot path.
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        original = _protected_artifacts(database)
        window = MainWindow()
        service = None
        monkeypatch.setattr(window, "show_error", lambda *error: errors.append(error))
        try:
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            window.config.config["user_preference"]["confirm_close"] = False
            window.config.config["user_preference"]["confirm_close_all"] = False
            window.config.config["runtime_settings"]["del_grace_period"] = 0
            window.spinBox.setValue(0)
            assert window.load_file(str(database))
            wait_for(lambda: not window._database_load_active and not window._database_detail_active)
            service = window._trusted_read_service
            window.show()
            wait_for(lambda: all(window.RunList.run_preview_is_ready(guid) for guid in guids))
            yield window, guids
        finally:
            close_main_window(window)
            assert service is None or service.closed
            assert _protected_artifacts(database) == original
            assert errors == []


def _drain_launches(window):
    wait_for(lambda: not getattr(window, "_trusted_plot_pending", ())
             and window.windows
             and all(hasattr(plot, "axis_data") and not plot.worker.running
                     for plot in window.windows))


def _click_row(window, guid):
    item = window.RunList._item_for_guid(guid)
    position = window.RunList.visualItemRect(item).center()
    position.setX(8)
    QtTest.QTest.mouseClick(window.RunList.viewport(), QtCore.Qt.MouseButton.LeftButton,
                           pos=position)


def _launch_then_select(window, guids, selection):
    first, second = guids
    label = next(label for label in window.RunList.preview_cells[first].findChildren(
        PreviewImageLabel) if label.parameter == "response")
    QtTest.QTest.mouseDClick(label, QtCore.Qt.MouseButton.LeftButton)
    assert len(window._trusted_plot_pending) == 1
    if selection == "second":
        _click_row(window, second)
    elif selection == "cleared":
        window.RunList.clearSelection()
    _drain_launches(window)
    assert len(window.windows) == 1
    assert window.windows[0].ds.guid == first
    np.testing.assert_array_equal(window.windows[0].axis_data["y"], [59., 51., 54., 75., 114.])


@pytest.mark.parametrize("selection", ["second", "cleared", "first"])
def test_thumbnail_completion_preserves_latest_selection(preview_window, selection):
    window, guids = preview_window
    _launch_then_select(window, guids, selection)
    expected = None if selection == "cleared" else guids[selection == "second"]
    assert window._selected_run_guid == expected
    assert window.infoBox.preview.current_guid == expected
    assert window.selected_run_id == (None if expected is None else 1 + (selection == "second"))
    assert window.ds is None


@pytest.mark.parametrize("action", ["preview_csv", "selected_all"])
def test_selected_actions_use_latest_row_after_thumbnail_completion(
    preview_window, tmp_path, monkeypatch, action,
):
    window, guids = preview_window
    _launch_then_select(window, guids, "second")
    if action == "preview_csv":
        destination = tmp_path / "selected-preview.csv"
        monkeypatch.setattr(window, "_choose_csv_export_filename", lambda _name:
                            prepare_export_destination(window, str(destination)))
        window.infoBox.preview.exportRequested.emit("response")
        with destination.open(newline="", encoding="utf-8") as exported:
            rows = list(csv.reader(exported))
        assert rows[0] == ["response", "drive"]
        np.testing.assert_array_equal(np.asarray(rows[1:], dtype=float), [
            [509., -3.], [501., -1.], [504., 2.], [525., 5.], [564., 8.],
        ])
    else:
        command = next(action for action in window.findChildren(QtGui.QAction)
                       if action.text() == "Plot All Measurements in Selected Run")
        command.trigger()
        _drain_launches(window)
        assert {plot.ds.guid for plot in window.windows} == set(guids)
        latest = next(plot for plot in window.windows if plot.ds.guid == guids[1])
        np.testing.assert_array_equal(latest.axis_data["y"], [509., 501., 504., 525., 564.])
