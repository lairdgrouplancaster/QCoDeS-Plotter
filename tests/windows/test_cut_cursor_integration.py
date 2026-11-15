"""Cursor toolbar indices in real heatmap cuts use full processed samples."""


import numpy as np
import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from tests.support.windows.cut_cursor_integration import (
    heatmap_cut as heatmap_cut,
)
from tests.support.windows.plot_integration import (
    wait_for,
)


def test_cut_snaps_to_recorded_sample(heatmap_cut):
    _, cut = heatmap_cut
    cut.snap_to_trace_action.setChecked(True)
    x, y = cut.line.getData()
    cut.line.getViewBox().autoRange()
    qtw.QApplication.processEvents()
    point = cut.line.getViewBox().mapViewToScene(QtCore.QPointF(float(x[1]), float(y[1])))
    cut.mouseMoved(point)
    assert cut.pos_labels["index"].text().replace(" ", "") == "[1]"
    assert float(cut.pos_labels["x"].text()) == pytest.approx(x[1])
    assert float(cut.pos_labels["y"].text()) == pytest.approx(y[1])
    assert cut.snap_marker is not None


def assert_toolbar_index(cut, x, index, *, x_range=None):
    if x_range is None:
        x_range = [x - 1, x + 1]
    cut.vb.setRange(xRange=x_range, yRange=[-1, 1], padding=0)
    qtw.QApplication.processEvents()
    pos = cut.vb.mapViewToScene(QtCore.QPointF(float(x), 0))
    assert cut.plot.sceneBoundingRect().contains(pos)
    cut.mouseMoved(pos)
    assert cut.pos_labels["index"].text().replace(" ", "") == ("" if index is None else f"[{index}]")


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
    assert heatmap.pos_labels["index"].text().replace(" ", "") == "[2,3]"


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
