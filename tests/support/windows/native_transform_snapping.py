"""Shared fixtures and assertions for native transform snapping."""

import numpy as np
import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.windows._plot1d_snap import _line_snap_data
from qplot.windows._plot_data_viewbox import coordinate_viewbox

__all__ = [
    'assert_full_snap_data',
    'assert_snap',
    'set_sample_range',
]


def assert_snap(window, owner, key, physical_x, physical_y, view_x, view_y, index):
    point = QtCore.QPointF(float(view_x[index]), float(view_y[index]))
    scene_pos = owner.mapViewToScene(point)
    assert window.plot.sceneBoundingRect().contains(scene_pos)
    window.plot.scene().sigMouseMoved.emit(scene_pos)
    assert window.pos_labels["x"].text() == f"{window.format_cursor_number(physical_x[index])}"
    assert window.pos_labels["y"].text() == f"{window.format_cursor_number(physical_y[index])}"
    assert window.pos_labels["index"].text().replace(" ", "") == f"[{index}]"
    assert "snapped to point" not in window.trace_label.text()
    nearest = window._nearest_trace_point(scene_pos)
    assert nearest.label == key
    assert coordinate_viewbox(nearest.viewbox) is coordinate_viewbox(owner)
    assert nearest.point_number == index + 1
    assert nearest.x_value == pytest.approx(physical_x[index])
    assert nearest.y_value == pytest.approx(physical_y[index])
    assert nearest.x_view_value == pytest.approx(view_x[index])
    assert nearest.y_view_value == pytest.approx(view_y[index])
    assert coordinate_viewbox(window._snap_marker_view) is coordinate_viewbox(owner)
    assert window.snap_marker.parentItem() is window._snap_marker_view.childGroup
    np.testing.assert_allclose(window.snap_marker.getData(), [[view_x[index]], [view_y[index]]])


def assert_full_snap_data(line, physical_x, physical_y, view_x, view_y):
    data = _line_snap_data(line)
    assert data is not None
    for actual, expected in zip((data.x_raw, data.y_raw, data.x_view, data.y_view),
                                (physical_x, physical_y, view_x, view_y), strict=True):
        assert len(actual) == len(physical_x)
        np.testing.assert_allclose(actual, expected, equal_nan=True, atol=1e-14)


def set_sample_range(owner, view_x, view_y, start, stop):
    x_span = view_x[stop] - view_x[start]
    y_low, y_high = np.nanmin(view_y), np.nanmax(view_y)
    y_margin = max((y_high - y_low) * 0.05, 0.1)
    owner.setRange(xRange=[view_x[start] - x_span * 0.01, view_x[stop] + x_span * 0.01],
                   yRange=[y_low - y_margin, y_high + y_margin], padding=0)
    qtw.QApplication.processEvents()
