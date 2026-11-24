"""Main commands follow the committed source, run target and loading lifecycle."""

import sys
from contextlib import closing

import pytest
from PyQt6 import QtTest, QtWidgets
from qcodes.dataset import initialise_or_create_database_at, load_or_create_experiment
from qcodes.dataset.sqlite.database import connect

from qplot.configuration.config import config
from qplot.diagnostics import configure_logging
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.support.windows.pending_close_all_publication import (
    _protected_artifacts,
    _record_run,
)
from tests.support.windows.plot_integration import configure_temp_qplot, wait_for

DATABASE_CONTROLS = (
    "copyDatabasePathButton", "databaseInfoButton", "openDatabaseFolderButton",
    "openDatabaseFolderAction", "closeDatabaseAction", "exportDatabaseAction", "autoPlotBox",
)
REFRESH_CONTROLS = ("refreshDatabaseButton", "refreshAction", "spinBox", "refreshIntervalLabel")
RUN_CONTROLS = ("run_idBox", "measurementBox", "plotRunButton", "exportCsvButton")
SELECTION_ACTIONS = ("exportSelectedRunsAction", "exportSelectedDatabaseAction",
                     "plotSelectedRunAction", "plotTableSelectionAction")


@pytest.fixture
def window(tmp_path, monkeypatch, qapplication):
    configure_temp_qplot(monkeypatch, tmp_path)
    configure_logging(tmp_path / "qplot.log", force=True)
    config().update_many({"updates.check_on_launch": False,
                         "user_preference.confirm_close": False,
                         "user_preference.confirm_close_all": False,
                         "user_preference.default_refresh_rate": 0})
    monkeypatch.setattr(MainWindow, "load_startup_database", lambda self: False)
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    window = MainWindow()
    window.startupDatabaseTimer.stop()
    monkeypatch.setattr(window, "show_error", lambda *error: errors.append(error))
    try:
        yield window
    finally:
        close_main_window(window)
        assert not errors


def assert_no_database_actions(window):
    for name in DATABASE_CONTROLS + REFRESH_CONTROLS + RUN_CONTROLS + SELECTION_ACTIONS + (
        "plotEnteredRunAction", "closeAllPlotsButton", "closeAllPlotsAction",
        "RunList", "infoBox", "runIdLabel", "measurementLabel",
    ):
        assert not getattr(window, name).isEnabled(), name
    assert all(not action.isEnabled() for action in window.open_param_actions)


def test_empty_window_keeps_one_load_button_and_useful_help(window):
    assert_no_database_actions(window)
    assert window.refreshAction.text() == "&Refresh runs and live plots"
    assert [shortcut.toString() for shortcut in window.refreshAction.shortcuts()] == ["F5", "R"]
    assert window.loadDatabaseButton.isEnabled()
    assert window.fileTextbox.isEnabled() and window.fileTextbox.acceptDrops()
    assert window.emptyStateHelpButton.isEnabled() and window.emptyStateHelpButton.isVisible()
    assert "Load database" in window.emptyStateDetail.text()
    buttons = window.emptyStateFrame.findChildren(QtWidgets.QToolButton)
    assert not any("load" in button.text().lower() for button in buttons)
    assert not hasattr(window, "emptyStateLoadButton")
    assert window.refreshStateLabel.text() == "Auto-refresh: unavailable"


def test_commands_follow_first_measurement_selection_reload_and_close(window, tmp_path, monkeypatch):
    database = (tmp_path / "availability.db").resolve()
    initialise_or_create_database_at(str(database), journal_mode="WAL")
    with closing(connect(str(database))) as writer:
        experiment = load_or_create_experiment("availability", "controls", conn=writer)
        original = _protected_artifacts(database)
        assert window.load_file(str(database))
        wait_for(lambda: not window._database_load_active)
        assert window.RunList.topLevelItemCount() == 0
        assert window.RunList.isEnabled()
        assert not window.infoBox.isEnabled()
        assert window.refreshStateLabel.text() == "Auto-refresh: off"
        for name in DATABASE_CONTROLS + REFRESH_CONTROLS:
            assert getattr(window, name).isEnabled(), name
        for name in RUN_CONTROLS + SELECTION_ACTIONS:
            assert not getattr(window, name).isEnabled(), name
        assert _protected_artifacts(database) == original

        guids = [_record_run(writer, experiment, database, offset) for offset in (11., 31.)]
        original = _protected_artifacts(database)
        window.refreshMain()
        wait_for(lambda: not window._database_refresh_active and window.RunList.topLevelItemCount() == 2)
        window.RunList.setCurrentItem(window.RunList._item_for_guid(guids[0]))
        assert window.infoBox.isEnabled()
        for name in RUN_CONTROLS + SELECTION_ACTIONS:
            assert getattr(window, name).isEnabled(), name

        window.run_idBox.setFocus()
        window.run_idBox.selectAll()
        QtTest.QTest.keyClicks(window.run_idBox, "999")
        assert not window.plotRunButton.isEnabled()
        assert not window.exportCsvButton.isEnabled()
        assert not window.exportSelectedRunsAction.isEnabled()
        window.run_idBox.selectAll()
        QtTest.QTest.keyClicks(window.run_idBox, "1")
        assert window.plotRunButton.isEnabled() and window.exportCsvButton.isEnabled()
        window.measurementBox.setText("invalid")
        assert not window.plotRunButton.isEnabled() and not window.exportCsvButton.isEnabled()
        window.measurementBox.setText("*")

        window.RunList.selectAll()
        assert not window.infoBox.isEnabled()
        assert window.exportSelectedRunsAction.isEnabled()
        assert window.exportSelectedDatabaseAction.isEnabled()
        assert not window.plotRunButton.isEnabled()
        assert not window.plotSelectedRunAction.isEnabled()

        # Hold the reload before its worker starts, then exercise cancellation.
        queued = []
        monkeypatch.setattr(window.databaseLoadThreadPool, "start", queued.append)
        assert window.load_file(str(database), force=True)
        assert queued and window._database_load_active
        window.measurementBox.setText("2")
        window.menuBar().actions()[0].menu().aboutToShow.emit()
        for name in REFRESH_CONTROLS + RUN_CONTROLS + SELECTION_ACTIONS:
            assert not getattr(window, name).isEnabled(), name
        window.cancel_database_load()
        assert not window._database_load_active
        assert window.refreshDatabaseButton.isEnabled()
        assert window.exportSelectedRunsAction.isEnabled()

        # Closing a source must not submit the focused field's pending edit.
        window.run_idBox.setFocus()
        window.run_idBox.selectAll()
        QtTest.QTest.keyClicks(window.run_idBox, "1")
        window.close_database()
        assert_no_database_actions(window)
        assert _protected_artifacts(database) == original
