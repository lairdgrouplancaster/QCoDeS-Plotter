"""Shared fixtures and assertions for plot dialog lifecycle."""

import os

import numpy as np
import pytest

from qplot.diagnostics import configure_logging
from qplot.windows._database_detail_state import detail_channel
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.support.windows.heatmap_layer_integration import (
    _assert_reader_artifact_invariant,
    _build_two_heatmap_database,
    _database_artifact_state,
)
from tests.support.windows.plot_csv_precision import _create_measurements
from tests.support.windows.plot_integration import configure_temp_qplot, wait_for

__all__ = [
    'dialog_plot',
]


@pytest.fixture
def dialog_plot(tmp_path, monkeypatch, request, qapplication):
    configure_logging(tmp_path / "qplot.log", force=True)
    configure_temp_qplot(monkeypatch, tmp_path)
    database = tmp_path / "dialog-lifecycle.db"
    mode = getattr(request, "param", "heatmap")
    if mode == "line":
        measurements = _create_measurements(
            database,
            [(np.arange(4.), np.arange(4.) * factor) for factor in (2, 3)],
        )
        guids = [guid for guid, _expected in measurements]
    else:
        _run_id, guid, _primary, _secondary = _build_two_heatmap_database(database)
        guids = [guid]
    original = _database_artifact_state(database)
    protected = {
        os.path.normcase(os.path.abspath(f"{database}{suffix}"))
        for suffix in ("", "-wal", "-shm", "-journal")
    }

    def guard_mutation(operation):
        def guarded(*args, **kwargs):
            for candidate in args[:2]:
                if isinstance(candidate, (str, bytes, os.PathLike)):
                    assert os.path.normcase(os.path.abspath(candidate)) not in protected
            return operation(*args, **kwargs)
        return guarded

    for name in ("remove", "unlink", "replace", "rename"):
        monkeypatch.setattr(os, name, guard_mutation(getattr(os, name)))
    window = MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.config.config["runtime_settings"]["del_grace_period"] = 0
        if mode == "downsample":
            window.config.config["runtime_settings"]["max_full_heatmap_points"] = 1
            window.config.config["runtime_settings"]["max_heatmap_grid_cells"] = 4
            window.config.config["runtime_settings"]["max_heatmap_grid_side"] = 2
        window.close_database(status=False)
        assert window.load_file(str(database))
        wait_for(lambda: not window._database_load_active and not detail_channel(window).active)
        if mode == "line":
            window.openPlot(guid=guids[0])
        else:
            window.open_preview_plot("signal_a")
        wait_for(lambda: bool(window.windows))
        plot = window.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        yield window, plot, qapplication, guids
    finally:
        close_main_window(window)
        _assert_reader_artifact_invariant(database, original)
