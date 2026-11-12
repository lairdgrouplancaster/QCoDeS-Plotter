"""Leaving a complex sample restores the free cursor's presentation."""

import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.windows._complex_plots import format_complex_cursor
from qplot.windows._plot1d_snap import SNAP_RADIUS_PIXELS
from tests.windows.test_complex_plots import complex_window as complex_window
from tests.windows.test_complex_plots import open_plot


@pytest.mark.parametrize("snapping", [False, True])
def test_complex_readout_clears_beyond_snap_radius(complex_window, snapping):
    window, guid, params, values = complex_window
    plot = open_plot(window, guid, params["S11"])
    plot.vb.setRange(xRange=(3, 8), yRange=(0, 10), padding=0)
    qtw.QApplication.processEvents()
    plot.snap_to_trace_action.setChecked(snapping)
    default_tooltip = plot._cursor_readout_default_tooltip
    point = plot.vb.mapViewToScene(QtCore.QPointF(6, float(abs(values[2]))))

    plot.mouseMoved(point)
    assert plot.pos_labels["y"].text() == format_complex_cursor(plot, values[2])
    assert "recorded complex value" in plot.cursor_readout.toolTip()
    assert plot._hover_highlight.active_item is plot.line

    distant = point + QtCore.QPointF(0, -SNAP_RADIUS_PIXELS - 20)
    assert plot.vb.sceneBoundingRect().contains(distant)
    plot.mouseMoved(distant)
    coordinates = plot.vb.mapSceneToView(distant)
    assert float(plot.pos_labels["x"].text()) == pytest.approx(coordinates.x(), rel=.006)
    assert float(plot.pos_labels["y"].text()) == pytest.approx(coordinates.y(), rel=.006)
    assert plot.cursor_readout.toolTip() == default_tooltip
    assert plot._hover_highlight.active_item is None
    assert plot._hover_highlight.axis_sides == ()
    assert plot._snap_marker_view is None
