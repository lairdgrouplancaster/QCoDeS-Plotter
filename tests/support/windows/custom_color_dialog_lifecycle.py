"""Shared fixtures and assertions for custom color dialog lifecycle."""

import sys

import pytest

from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.support.windows.heatmap_layer_integration import (
    _assert_reader_artifact_invariant,
    _build_two_heatmap_database,
    _configure_temp_qplot,
    _database_artifact_state,
    _wait_for,
)

__all__ = [
    'color_cut',
]


@pytest.fixture
def color_cut(tmp_path, monkeypatch):
    _configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "custom-color.db"
    _run_id, guid, _primary, _secondary = _build_two_heatmap_database(path)
    protected = {path: _database_artifact_state(path)}
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    window = MainWindow()
    monkeypatch.setattr(window, "show_error", lambda *error: errors.append(error))
    service = None
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.config.config["runtime_settings"]["del_grace_period"] = 0
        assert window.load_file(str(path))
        _wait_for(lambda: not window._database_load_active)
        service = window._trusted_read_service
        window.open_run_preview_plot(guid, "signal_a")
        _wait_for(lambda: len(window.windows) == 1)
        heatmap = window.windows[0]
        _wait_for(lambda: "dataGrid" in heatmap.__dict__ and not heatmap.worker.running)
        heatmap.spinBox.setValue(0)
        heatmap.z_index = [1, 1]
        heatmap.openSweep("h")
        cut = window.windows[-1]
        cut.spinBox.setValue(0)
        yield window, heatmap, cut, protected
    finally:
        close_main_window(window)
        assert service is None or service.closed
        for database, original in protected.items():
            _assert_reader_artifact_invariant(database, original)
        assert not errors
