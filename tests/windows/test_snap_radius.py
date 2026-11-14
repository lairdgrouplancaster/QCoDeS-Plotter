"""Trace snapping stays local to the pointer in logical screen pixels."""

import math

import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from tests.windows.test_mixed_plot_drops import _added, _drop
from tests.windows.test_mixed_plot_drops import (
    mixed_plot as mixed_plot,
)
from tests.windows.test_native_transform_labels import (
    no_callback_errors as no_callback_errors,
)
from tests.windows.test_plot_overlay_integration import _assign_trace_axes
from tests.windows.test_plot_overlay_integration import (
    loaded_plot as loaded_plot,
)


def _sample_scene_point(line, index):
    x, y = line.getData()
    return line.getViewBox().mapViewToScene(QtCore.QPointF(float(x[index]), float(y[index])))


def _normal_to_trace(line, index):
    tangent = _sample_scene_point(line, index + 1) - _sample_scene_point(line, index)
    length = math.hypot(tangent.x(), tangent.y())
    return QtCore.QPointF(-tangent.y() / length, tangent.x() / length)


@pytest.mark.parametrize("loaded_plot", [False, True], indirect=True, ids=["primary", "secondary"])
@pytest.mark.parametrize("scatter", [False, True], ids=["line", "scatter"])
def test_snap_radius_stays_in_screen_pixels_across_zoom_and_axes(
    loaded_plot, no_callback_errors, scatter,
):
    window = loaded_plot
    key, line = next(
        ((key, line) for key, line in window.lines.items() if line is not window.line),
        (window.label, window.line),
    )
    secondary = line is not window.line
    axes = ("Top", "Right") if secondary else ("Bottom", "Left")
    style = window._trace_styles[key]
    style.line_enabled = not scatter
    style.markers_enabled = scatter
    style.markers_size = 6.0
    owner = _assign_trace_axes(window, key, *axes)
    if secondary:
        # The main trace's independent scale puts its samples well away from
        # the inspected secondary trace, despite identical recorded values.
        window.plot.vb.setRange(xRange=[-10, 80], yRange=[1000, 2000], padding=0)

    for size, x_range, y_range in (
        ((900, 650), [-10, 80], [0, 100]),
        ((1200, 800), [15, 25], [25, 35]),
    ):
        window.resize(*size)
        owner.setRange(xRange=x_range, yRange=y_range, padding=0)
        qtw.QApplication.processEvents()
        sample = _sample_scene_point(line, 20)
        normal = _normal_to_trace(line, 20)
        inside = sample + normal * 11
        outside = sample + normal * 13
        assert window.plot.vb.sceneBoundingRect().contains(inside)
        assert window.plot.vb.sceneBoundingRect().contains(outside)

        # Moving back into range must immediately reacquire the same sample.
        for pointer in (inside, outside, inside):
            window.plot.scene().sigMouseMoved.emit(pointer)
            nearest = window._nearest_trace_point(pointer)
            if pointer == outside:
                assert nearest is None
                assert window._hover_highlight.active_item is None
                assert window._hover_highlight.axis_sides == ()
                assert window._hover_highlight.effects == ()
                assert window.snap_marker.scene() is None
                free_point = window.line.getViewBox().mapSceneToView(pointer)
                assert window.pos_labels["x"].text() == window.format_cursor_number(free_point.x())
                assert window.pos_labels["y"].text() == window.format_cursor_number(free_point.y())
            else:
                assert nearest.label == key
                assert nearest.point_number == 21
                assert window._hover_highlight.active_item is line
                assert window._hover_highlight.axis_sides == tuple(side.lower() for side in axes)
                assert window.snap_marker.scene() is window.plot.scene()
                assert window.pos_labels["index"].text().replace(" ", "") == "[20]"
                assert float(window.pos_labels["x"].text()) == 20
                assert float(window.pos_labels["y"].text()) == 30


def test_snap_radius_is_circular_around_an_isolated_sample(loaded_plot, no_callback_errors):
    window = loaded_plot
    window.line.setData([20.0], [30.0])
    window.plot.vb.setRange(xRange=[15, 25], yRange=[25, 35], padding=0)
    qtw.QApplication.processEvents()
    sample = _sample_scene_point(window.line, 0)

    assert window._nearest_trace_point(sample + QtCore.QPointF(11, 0)) is not None
    assert window._nearest_trace_point(sample + QtCore.QPointF(0, -11)) is not None
    assert window._nearest_trace_point(sample + QtCore.QPointF(9, 9)) is None
    assert window._nearest_trace_point(sample + QtCore.QPointF(-9, -9)) is None


@pytest.mark.parametrize("mixed_plot", ["heatmap"], indirect=True)
def test_distant_trace_yields_to_heatmap_without_disabling_snapping(mixed_plot):
    _main, window, runs = mixed_plot
    _drop(window, runs["line"])
    line = _added(window, runs["line"])
    window.show()
    window.plot.vb.setRange(xRange=[-0.5, 2.5], yRange=[-2, 25], padding=0)
    qtw.QApplication.processEvents()
    assert window.snap_to_trace_action.isChecked()
    near_line = _sample_scene_point(line, 1) + QtCore.QPointF(0, 5)
    heatmap_point = window._primary_heatmap_viewbox().mapViewToScene(QtCore.QPointF(1, 10))

    for pointer in (near_line, heatmap_point, near_line):
        window.plot.scene().sigMouseMoved.emit(pointer)
        assert window.snap_to_trace_action.isChecked()
        if pointer == heatmap_point:
            assert window._nearest_trace_point(pointer) is None
            assert window._hover_highlight.active_item in window._heatmap_render_items(window)
            assert window._hover_highlight.outline.isVisible()
            assert window.snap_marker.scene() is None
            assert window.hover_pixel_outline.isVisible()
            assert not window.pos_labels["z"].isHidden()
            assert float(window.pos_labels["x"].text()) == 1
            assert float(window.pos_labels["y"].text()) == 10
            assert float(window.pos_labels["z"].text()) == 11
            assert window.pos_labels["index"].text().replace(" ", "") == "[1,0]"
        else:
            assert window._hover_highlight.active_item is line
            assert window.snap_marker.scene() is window.plot.scene()
            assert not window.hover_pixel_outline.isVisible()
            assert window.pos_labels["z"].isHidden()
            assert float(window.pos_labels["x"].text()) == 1
            assert float(window.pos_labels["y"].text()) == 1
            assert window.pos_labels["index"].text().replace(" ", "") == "[1]"
