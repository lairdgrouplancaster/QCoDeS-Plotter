"""Leaving a complex sample restores the free cursor's presentation."""

import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.complex_data import COMPLEX_1D_KEY
from qplot.windows._complex_plots import format_complex_cursor
from qplot.windows._plot1d_snap import SNAP_RADIUS_PIXELS
from tests.support.windows.complex_plots import complex_window as complex_window
from tests.support.windows.complex_plots import open_plot


@pytest.mark.parametrize("snapping", [False, True])
@pytest.mark.parametrize("projection", ["imag_vs_real", "magnitude"])
def test_complex_readout_clears_beyond_snap_radius(complex_window, snapping, projection):
    window, guid, params, values = complex_window
    window.config.update(COMPLEX_1D_KEY, projection)
    plot = open_plot(window, guid, params["S11"])
    sample = values[2]
    x, y = (sample.real, sample.imag) if projection == "imag_vs_real" else (6, abs(sample))
    low = min(values.imag if projection == "imag_vs_real" else abs(values))
    plot.vb.setRange(xRange=(x - 3, x + 3), yRange=(low - 6, y + 5), padding=0)
    qtw.QApplication.processEvents()
    plot.snap_to_trace_action.setChecked(snapping)
    default_tooltip = plot._cursor_readout_default_tooltip
    point = plot.vb.mapViewToScene(QtCore.QPointF(x, y))

    plot.mouseMoved(point)
    assert plot.pos_labels["y"].text() == format_complex_cursor(plot, values[2])
    assert "recorded complex value" in plot.cursor_readout.toolTip()
    assert plot._hover_highlight.active_item is plot.line

    # Move below the entire trace, including segments between Argand samples.
    distant = plot.vb.mapViewToScene(QtCore.QPointF(x, low - 4))
    lowest = plot.vb.mapViewToScene(QtCore.QPointF(x, low))
    assert distant.y() - lowest.y() > SNAP_RADIUS_PIXELS
    assert plot.vb.sceneBoundingRect().contains(distant)
    plot.mouseMoved(distant)
    coordinates = plot.vb.mapSceneToView(distant)
    assert float(plot.pos_labels["x"].text()) == pytest.approx(coordinates.x(), rel=.006)
    assert float(plot.pos_labels["y"].text()) == pytest.approx(coordinates.y(), rel=.006)
    assert plot.cursor_readout.toolTip() == default_tooltip
    assert plot._hover_highlight.active_item is None
    assert plot._hover_highlight.axis_sides == ()
    assert plot._snap_marker_view is None
