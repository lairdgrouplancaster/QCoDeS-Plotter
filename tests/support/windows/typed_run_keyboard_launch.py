"""Shared fixtures and assertions for typed run keyboard launch."""

import sys
from contextlib import closing

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
from tests.support.windows.pending_close_all_publication import _protected_artifacts
from tests.support.windows.plot_integration import configure_temp_qplot, wait_for

__all__ = [
    '_drain',
    '_launch',
    '_type_run',
    'typed_run_window',
]


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
                saver.dataset.add_metadata("calibration", f"Reference offset {offset}")
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
