"""Shared fixtures and assertions for cut cursor integration."""

from contextlib import closing

import pytest
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.readonly import sqlite_read_only_connection
from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.support.windows.plot_integration import (
    build_synthetic_database,
    configure_temp_qplot,
    database_artifact_state,
    wait_for,
)

__all__ = [
    'heatmap_cut',
]


@pytest.fixture
def heatmap_cut(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path)
    database_path = tmp_path / "cut-cursor.db"
    _, run_id = build_synthetic_database(database_path)
    protected = database_artifact_state(database_path)
    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(lambda: not window._database_load_active)
        with closing(sqlite_read_only_connection(database_path)) as connection:
            guid = connection.execute(
                "SELECT guid FROM runs WHERE run_id = ?", (run_id,),
            ).fetchone()[0]
        prior_plot_count = len(window.windows)
        window.openPlot(guid=guid, show=True)
        wait_for(lambda prior_plot_count=prior_plot_count: len(window.windows) > prior_plot_count)
        heatmap = window.windows[-1]
        wait_for(lambda: hasattr(heatmap, "dataGrid") and not heatmap.worker.running)
        heatmap.monitor.stop()
        heatmap.z_index = [2, 1]
        heatmap.openSweep("h")
        cut = window.windows[-1]
        wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
        cut.monitor.stop()
        assert cut.snap_to_trace_action.isChecked()
        cut.snap_to_trace_action.setChecked(False)
        cut.show()
        qtw.QApplication.processEvents()
        yield heatmap, cut
    finally:
        close_main_window(window)
        assert database_artifact_state(database_path) == protected
