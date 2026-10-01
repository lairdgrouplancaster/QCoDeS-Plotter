"""Cursor toolbar indices in real heatmap cuts use full processed samples."""

from contextlib import closing

import numpy as np
import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.readonly import sqlite_read_only_connection
from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import (
    build_synthetic_database,
    configure_temp_qplot,
    database_artifact_state,
    wait_for,
)


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
        window.openPlot(guid=guid, show=True)
        heatmap = window.windows[-1]
        wait_for(lambda: hasattr(heatmap, "dataGrid") and not heatmap.worker.running)
        heatmap.monitor.stop()
        heatmap.z_index = [2, 1]
        heatmap.openSweep("h")
        cut = window.windows[-1]
        wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
        cut.monitor.stop()
        cut.show()
        qtw.QApplication.processEvents()
        yield heatmap, cut
    finally:
        close_main_window(window)
        assert database_artifact_state(database_path) == protected


def assert_toolbar_index(cut, x, index, *, x_range=None):
    if x_range is None:
        x_range = [x - 1, x + 1]
    cut.vb.setRange(xRange=x_range, yRange=[-1, 1], padding=0)
    qtw.QApplication.processEvents()
    pos = cut.vb.mapViewToScene(QtCore.QPointF(float(x), 0))
    assert cut.plot.sceneBoundingRect().contains(pos)
    cut.mouseMoved(pos)
    assert cut.pos_labels["index"].text() == ("" if index is None else f"[{index}]")


@pytest.mark.parametrize("method", ["subsample", "mean", "peak"])
@pytest.mark.parametrize("coordinates,logarithmic", [
    ([-0.6, -0.3, 0, 0.3, 0.6], False),
    ([0.2, 0.3, 1.7, 2, 11], False),
    ([11, 2, 1.7, 0.3, 0.2], False),
    ([1, 10, 100, 1000, 10000], True),
])
def test_cut_toolbar_uses_full_samples(heatmap_cut, method, coordinates, logarithmic):
    _, cut = heatmap_cut
    # Feed coordinates through the cut's normal trace update path.
    cut.axis_data["x"] = np.asarray(coordinates, dtype=float)
    cut.update_sweep()
    cut.plot.setLogMode(x=logarithmic)
    target = np.log10(coordinates[-1]) if logarithmic else coordinates[-1]
    assert_toolbar_index(cut, target, 4)
    cut.plot.setDownsampling(ds=2, auto=False, mode=method)
    assert len(cut.line.getData()[0]) < len(coordinates)
    assert_toolbar_index(cut, target, 4)
    # Log-space nearest differs from physical-space nearest here.
    if logarithmic:
        assert_toolbar_index(cut, 3.7, 4)
    cut.plot.setDownsampling(ds=False, auto=False)
    assert_toolbar_index(cut, target, 4)


def test_cut_automatic_downsampling_and_clipping(heatmap_cut):
    _, cut = heatmap_cut
    cut.widget.setFixedWidth(600)
    x = np.arange(12000, dtype=float)
    cut.line.setData(x=x, y=np.sin(x))
    cut.plot.setDownsampling(auto=True, mode="peak")
    assert_toolbar_index(cut, 8443, 8443, x_range=[0, 12000])
    assert len(cut.line.getData()[0]) < len(x)
    cut.plot.ctrl.clipToViewCheck.setChecked(True)
    assert_toolbar_index(cut, 8443, 8443, x_range=[8440, 8450])
    assert len(cut.line.getData()[0]) < len(x)


@pytest.mark.parametrize("control,logarithmic", [
    ("fftCheck", False), ("fftCheck", True),
    ("subtractMeanCheck", False), ("derivativeCheck", False),
    ("phasemapCheck", False),
])
def test_cut_native_processing_indices(heatmap_cut, control, logarithmic):
    _, cut = heatmap_cut
    x = np.arange(64, dtype=float)
    cut.line.setData(x=x, y=x ** 2 + 1)
    getattr(cut.plot.ctrl, control).setChecked(True)
    cut.plot.setLogMode(x=logarithmic)
    full_x = cut.line.getData()[0].copy()
    index = 18
    assert_toolbar_index(cut, full_x[index], index)
    cut.plot.setDownsampling(ds=8, auto=False, mode="peak")
    assert len(cut.line.getData()[0]) < len(full_x)
    assert_toolbar_index(cut, full_x[index], index)


def test_cut_cursor_updates_after_moving_and_swapping_axes(heatmap_cut):
    heatmap, cut = heatmap_cut
    cut.plot.setDownsampling(ds=2, auto=False, mode="subsample")
    assert_toolbar_index(cut, cut.axis_data["x"][-1], 4)
    previous_y = cut.axis_data["y"].copy()
    cut.picker.slider.setValue(3)
    assert cut.fixed_index == 3
    assert not np.array_equal(cut.axis_data["y"], previous_y)
    assert_toolbar_index(cut, cut.axis_data["x"][-1], 4)
    previous_fixed = cut.fixed_indep
    cut.axis_dropdown["x"].setCurrentText(previous_fixed)
    wait_for(lambda: cut._axis_change_transaction is None and not cut.worker.running)
    assert cut.sweep_indep == previous_fixed
    assert len(cut.axis_data["x"]) == 7
    assert_toolbar_index(cut, cut.axis_data["x"][-1], 6)
    cut.picker.slider.setValue(2)
    assert_toolbar_index(cut, cut.axis_data["x"][-1], 6)
    # The parent heatmap retains its [column,row] coordinate readout.
    heatmap.mouseMoved(heatmap._primary_heatmap_viewbox().mapViewToScene(QtCore.QPointF(0, 0)))
    assert heatmap.pos_labels["index"].text() == "[2,3]"


@pytest.mark.parametrize("x,expected", [
    ([], None), ([np.nan, np.inf, -np.inf], None),
    ([np.nan, 0, np.inf, 2], 3),
])
def test_cut_empty_and_nonfinite_cursor(heatmap_cut, x, expected):
    _, cut = heatmap_cut
    cut.line.setData(x=np.asarray(x), y=np.zeros(len(x)))
    cut.plot.setDownsampling(ds=2, auto=False, mode="subsample")
    assert_toolbar_index(cut, 2, expected)
    assert cut._nearest_1d_array_index(float("nan")) is None
    assert cut._nearest_1d_array_index(float("inf")) is None
