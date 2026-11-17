"""Protected synthetic SQL heatmaps for publication integration tests."""

import sys
from contextlib import contextmanager

import numpy as np

from qplot.windows._heatmap_viewport import AxisRanges
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.support.windows.plot_integration import (
    assert_database_artifacts_unchanged_except_trusted_shm,
    build_synthetic_database,
    configure_temp_qplot,
    trusted_database_artifact_state,
    wait_for,
)


@contextmanager
def sql_heatmap(tmp_path, monkeypatch, *, grid_cells=None, show=False):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "viewport.db"
    build_synthetic_database(path)
    protected = trusted_database_artifact_state(path)
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: errors.append(args))
    window = MainWindow()
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.config.config["user_preference"].update(confirm_close=False, confirm_close_all=False)
    window.config.config["runtime_settings"]["max_full_heatmap_points"] = 4
    if grid_cells is not None:
        window.config.config["runtime_settings"]["max_heatmap_grid_cells"] = grid_cells
    monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
    try:
        loaded = window.load_file(str(path))
        assert loaded
        wait_for(lambda: not window._database_load_active and window._selected_run_guid is not None)
        window.openPlot(guid=window._selected_run_guid, show=show)
        wait_for(lambda: len(window.windows) == 1)
        plot = window.windows[0]
        plot.monitor.stop()
        idle(plot)
        assert plot.worker.result.loaded_from_sql_heatmap
        assert plot._viewport_state.enabled
        assert plot._viewport_state.full is not None
        yield window, plot
    finally:
        close_main_window(window)
        assert not errors
        assert_database_artifacts_unchanged_except_trusted_shm(path, protected)


def idle(plot):
    # A worker can finish before its queued publication callback reaches Qt.
    wait_for(lambda: not plot._refresh_controller().session.active
             and not plot._viewport_controller().pending)


def subset(full, low, high):
    return AxisRanges(*(tuple(a + (b - a) * f for f in (low, high)) for a, b in (full.x, full.y)))


def view(plot, ranges):
    # Choose Manual through the same handlers as the axis controls. A raw
    # ViewBox.setRange alone leaves qPlot's filtered Auto mode enabled.
    plot._axis_scale_manual_clicked("x")
    plot._axis_scale_manual_clicked("y")
    plot._primary_heatmap_viewbox().setRange(xRange=ranges.x, yRange=ranges.y, padding=0)


def assert_source_values(plot):
    coordinates = {plot.axis_param[axis].name: values for axis, values in plot.axis_data.items()}
    gate = coordinates["gate"]
    bias = coordinates["bias"]
    expected = np.cos(gate[:, None] * 2.) + np.sin(bias[None, :] * 3.)
    if plot.axis_param["x"].name == "gate":
        expected = expected.T
    np.testing.assert_allclose(plot.dataGrid, expected, equal_nan=True)


