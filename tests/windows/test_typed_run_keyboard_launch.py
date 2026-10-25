"""Commit typed run selection before asynchronous keyboard plot requests."""

import sys
from contextlib import closing

import numpy as np
import pytest
from PyQt6 import QtCore, QtTest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect

from qplot.diagnostics import configure_logging
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.windows.test_pending_close_all_publication import _protected_artifacts
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


@pytest.fixture
def typed_run_window(tmp_path, monkeypatch, qapplication):
    configure_temp_qplot(monkeypatch, tmp_path)
    configure_logging(tmp_path / "qplot.log", force=True)
    database = tmp_path / "typed-run.db"
    initialise_or_create_database_at(str(database), journal_mode="WAL")
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    with closing(connect(str(database))) as writer:
        experiment = load_or_create_experiment("typed run", "round 35", conn=writer)
        runs = []
        for offset in (13, 37):
            measurement = Measurement(exp=experiment)
            measurement.register_custom_parameter("gate")
            measurement.register_custom_parameter("signal", setpoints=("gate",))
            measurement.register_custom_parameter("reference", setpoints=("gate",))
            with measurement.run(write_in_background=False) as saver:
                for value in (-3, -1, 2, 4, 9):
                    saver.add_result(("gate", value), ("signal", offset + value),
                                     ("reference", offset + value * value))
            runs.append((saver.dataset.run_id, saver.dataset.guid, offset))
        original = _protected_artifacts(database)
        window = MainWindow()
        monkeypatch.setattr(window, "show_error", lambda *error: errors.append(error))
        service = None
        try:
            window.startupDatabaseTimer.stop()
            window.config.config["user_preference"]["default_refresh_rate"] = 0
            window.config.config["user_preference"]["confirm_close"] = False
            window.config.config["user_preference"]["confirm_close_all"] = False
            window.config.config["runtime_settings"]["del_grace_period"] = 0
            window.apply_current_settings()
            window.show()
            assert window.load_file(str(database))
            wait_for(lambda: not window._database_load_active
                     and window._selected_run_guid == runs[-1][1])
            service = window._trusted_read_service
            yield window, runs
        finally:
            close_main_window(window)
            assert service is None or service.closed
            assert _protected_artifacts(database) == original
            assert not errors


def _type_run(window, run_id):
    window.run_idBox.setFocus()
    window.run_idBox.selectAll()
    QtTest.QTest.keyClicks(window.run_idBox, str(run_id))
    window.measurementBox.setText("2")
    assert window.selected_run_id == run_id
    assert window._selected_run_guid is None
    assert not window.RunList.selectedItems()


def _launch(window, route):
    if route == "button":
        QtTest.QTest.mouseClick(window.plotRunButton, QtCore.Qt.MouseButton.LeftButton)
    elif route == "shortcut":
        QtTest.QTest.keyClick(window.run_idBox, QtCore.Qt.Key.Key_Return,
                             QtCore.Qt.KeyboardModifier.ControlModifier)
        # Moving focus immediately afterward is a normal keyboard workflow.
        window.measurementBox.setFocus()
    else:
        QtTest.QTest.keyClick(window.run_idBox, getattr(QtCore.Qt.Key, route))


def _drain(window):
    wait_for(lambda: not getattr(window, "_trusted_plot_pending", ())
             and all(hasattr(plot, "axis_data") and not plot.worker.running
                     for plot in window.windows))


@pytest.mark.parametrize("route", ["Key_Return", "Key_Enter", "shortcut", "button"])
@pytest.mark.parametrize("same_run", [False, True], ids=["different-run", "same-run"])
def test_typed_run_launch_survives_edit_completion(typed_run_window, route, same_run):
    window, runs = typed_run_window
    run_id, guid, offset = runs[-1 if same_run else 0]
    _type_run(window, run_id)
    _launch(window, route)
    _drain(window)
    assert len(window.windows) == 1
    plot = window.windows[0]
    plot.monitor.stop()
    assert plot.ds.guid == guid
    assert plot.param.name == "reference"
    np.testing.assert_array_equal(plot.axis_data["x"], [-3, -1, 2, 4, 9])
    np.testing.assert_array_equal(plot.axis_data["y"],
                                  offset + np.array([9, 1, 4, 16, 81]))
    assert window.RunList.currentItem().guid == guid


@pytest.mark.parametrize("change", ["selection", "typing", "close-all", "close-database"])
def test_typed_run_pending_launch_still_obeys_lifecycle(typed_run_window, change):
    window, runs = typed_run_window
    _type_run(window, runs[0][0])
    _launch(window, "Key_Return")
    assert window._trusted_plot_pending
    if change == "selection":
        window.RunList.setCurrentItem(window.RunList._item_for_guid(runs[1][1]))
    elif change == "typing":
        _type_run(window, runs[1][0])
    elif change == "close-all":
        window.closeAllPlotsButton.click()
    else:
        window.close_current_database()
    _drain(window)
    assert not window.windows
