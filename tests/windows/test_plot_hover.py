"""Hover identity follows the coordinate readout without changing measurements."""

from dataclasses import asdict

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from tests.windows.test_hover_axis_hit_testing import _viewport_move
from tests.windows.test_mixed_plot_drops import (
    _added,
    _drop,
)
from tests.windows.test_mixed_plot_drops import (
    mixed_plot as mixed_plot,
)
from tests.windows.test_native_transform_labels import (
    no_callback_errors as no_callback_errors,
)
from tests.windows.test_plot_integration import wait_for
from tests.windows.test_plot_overlay_integration import (
    _assign_trace_axes,
)
from tests.windows.test_plot_overlay_integration import (
    loaded_plot as loaded_plot,
)


def _move_to_sample(window, line, index):
    # Tiling can leave fixture windows too small for a data area at larger
    # platform fonts. Settle a usable viewport before choosing a scene point.
    window.resize(1000, 700)
    wait_for(lambda: window.width() == 1000 and window.height() == 700)
    x, y = line.getData()
    point = line.getViewBox().mapViewToScene(
        QtCore.QPointF(float(x[index]), float(y[index]))
    )
    assert window.plot.vb.sceneBoundingRect().contains(point)
    window.plot.scene().sigMouseMoved.emit(point)
    return point


def _assert_highlighted_axes(window, expected):
    assert window._hover_highlight.axis_sides == expected
    for side in ("bottom", "left", "top", "right"):
        effect = window.plot.getAxis(side).graphicsEffect()
        assert bool(effect is not None and effect.isEnabled()) == (side in expected)


@pytest.mark.parametrize("axes", [
    ("Bottom", "Left"), ("Bottom", "Right"),
    ("Top", "Left"), ("Top", "Right"),
])
@pytest.mark.parametrize("scatter", [False, True])
def test_trace_hover_identifies_assigned_axes_and_preserves_style(
    loaded_plot, no_callback_errors, axes, scatter,
):
    window = loaded_plot
    style = window._trace_styles[window.label]
    style.line_enabled = not scatter
    style.markers_enabled = scatter
    style.markers_size = 6.0
    style.opacity = 0.45
    owner = _assign_trace_axes(window, window.label, *axes)
    owner.setRange(xRange=[-1, 65], yRange=[0, 85], padding=0)
    qtw.QApplication.processEvents()
    line = window.line
    original = tuple(values.copy() for values in line.getOriginalDataset())
    original_style = asdict(style)
    opacity_effect = line.graphicsEffect()
    original_registry = list(window.plot.listDataItems())
    original_bounds = [line.dataBounds(dimension) for dimension in (0, 1)]

    _move_to_sample(window, line, 31)

    hover = window._hover_highlight
    assert hover.active_item is line
    _assert_highlighted_axes(window, tuple(side.lower() for side in axes))
    assert window.pos_labels["index"].text().replace(" ", "") == "[31]"
    assert float(window.pos_labels["x"].text()) == 31
    assert float(window.pos_labels["y"].text()) == 41
    assert any(effect.isEnabled() for effect in hover.effects)
    assert line.scatter.isVisible() == scatter
    assert (line.curve.opts["pen"].style() == QtCore.Qt.PenStyle.NoPen) == scatter
    assert line.graphicsEffect() is opacity_effect
    assert opacity_effect.opacity() == 0.45
    assert asdict(style) == original_style
    assert window.plot.listDataItems() == original_registry
    assert [line.dataBounds(dimension) for dimension in (0, 1)] == original_bounds
    for before, after in zip(original, line.getOriginalDataset(), strict=True):
        np.testing.assert_array_equal(before, after)

    window._clear_cursor_hover()
    assert hover.active_item is None
    _assert_highlighted_axes(window, ())
    assert line.graphicsEffect() is opacity_effect
    assert asdict(style) == original_style


@pytest.mark.parametrize("loaded_plot", [True], indirect=True)
def test_switching_trace_moves_highlight_with_readout(loaded_plot, no_callback_errors):
    window = loaded_plot
    secondary_key, secondary = next(
        (key, line) for key, line in window.lines.items() if line is not window.line
    )
    owner = _assign_trace_axes(window, secondary_key, "Top", "Right")
    window.plot.vb.setRange(xRange=[-1, 65], yRange=[0, 160], padding=0)
    owner.setRange(xRange=[-1, 65], yRange=[-80, 80], padding=0)
    qtw.QApplication.processEvents()

    _move_to_sample(window, window.line, 20)
    assert window._hover_highlight.active_item is window.line
    _assert_highlighted_axes(window, ("bottom", "left"))
    _move_to_sample(window, secondary, 20)
    assert window._hover_highlight.active_item is secondary
    _assert_highlighted_axes(window, ("top", "right"))
    assert window.pos_labels["index"].text().replace(" ", "") == "[20]"
    assert float(window.pos_labels["y"].text()) == 30
    assert not any(
        item.graphicsEffect() is not None and item.graphicsEffect().isEnabled()
        for item in (window.line.curve, window.line.scatter)
    )


def test_free_cursor_has_no_trace_or_axis_highlight(loaded_plot, no_callback_errors):
    window = loaded_plot
    owner = _assign_trace_axes(window, window.label, "Top", "Right")
    owner.setRange(xRange=[-1, 65], yRange=[0, 85], padding=0)
    qtw.QApplication.processEvents()
    _move_to_sample(window, window.line, 20)
    assert window._hover_highlight.active_item is window.line

    window.snap_to_trace_action.setChecked(False)
    _move_to_sample(window, window.line, 20)
    assert window._hover_highlight.active_item is None
    _assert_highlighted_axes(window, ())


@pytest.mark.parametrize("scatter", [False, True])
def test_empty_plot_interior_clears_trace_and_axes_until_data_is_inspected(
    loaded_plot, no_callback_errors, monkeypatch, scatter,
):
    monkeypatch.setitem(pg.CONFIG_OPTIONS, "mouseRateLimit", 0)
    window = loaded_plot
    style = window._trace_styles[window.label]
    style.line_enabled = not scatter
    style.markers_enabled = scatter
    style.markers_size = 6.0
    window._apply_trace_style(window.label, window.line)
    window.resize(1000, 700)
    owner = window.line.getViewBox()
    owner.setRange(xRange=[-5, 70], yRange=[0, 85], padding=0)
    qtw.QApplication.processEvents()
    data_point = owner.mapViewToScene(QtCore.QPointF(31, 41))
    empty_point = owner.mapViewToScene(QtCore.QPointF(10, 70))
    assert window._cursor_inside_plot(empty_point)
    assert window._nearest_trace_point(empty_point) is None

    def assert_no_emphasis():
        assert window._hover_highlight.active_item is None
        _assert_highlighted_axes(window, ())
        assert window._hover_highlight.effects == ()
        assert window._hover_highlight.warning_badges == {}
        assert window.snap_marker.scene() is None

    _viewport_move(window, data_point)
    assert window._hover_highlight.active_item is window.line
    _assert_highlighted_axes(window, ("bottom", "left"))
    actual = _viewport_move(window, empty_point)
    assert_no_emphasis()
    free = owner.mapSceneToView(actual)
    assert window.pos_labels["x"].text() == window.format_cursor_number(free.x())
    assert window.pos_labels["y"].text() == window.format_cursor_number(free.y())
    _viewport_move(window, data_point)
    assert window._hover_highlight.active_item is window.line
    _assert_highlighted_axes(window, ("bottom", "left"))

    window.snap_to_trace_action.setChecked(False)
    assert_no_emphasis()
    for point in (empty_point, data_point):
        _viewport_move(window, point)
        assert_no_emphasis()
    window.snap_to_trace_action.setChecked(True)
    assert window._hover_highlight.active_item is window.line
    _assert_highlighted_axes(window, ("bottom", "left"))


@pytest.mark.parametrize("leave", ["axes", "viewport"])
def test_hover_clears_when_pointer_leaves_data_area(loaded_plot, no_callback_errors, leave):
    window = loaded_plot
    _move_to_sample(window, window.line, 20)
    assert window._hover_highlight.active_item is window.line
    if leave == "axes":
        window.plot.scene().sigMouseMoved.emit(
            window.plot.getAxis("bottom").sceneBoundingRect().center()
        )
    else:
        qtw.QApplication.sendEvent(
            window.widget.viewport(), QtCore.QEvent(QtCore.QEvent.Type.Leave)
        )
    assert window._hover_highlight.active_item is None
    _assert_highlighted_axes(window, ())


def test_hiding_active_trace_clears_highlight(loaded_plot, no_callback_errors):
    window = loaded_plot
    _move_to_sample(window, window.line, 20)
    assert window._hover_highlight.active_item is window.line
    style = window._trace_styles[window.label]
    style.visible = False
    window._apply_trace_style(window.label, window.line)
    qtw.QApplication.processEvents()
    assert window._hover_highlight.active_item is None
    _assert_highlighted_axes(window, ())


def test_stationary_cursor_follows_trace_style_changes(loaded_plot, no_callback_errors):
    window = loaded_plot
    _assign_trace_axes(window, window.label, "Top", "Right")
    point = _move_to_sample(window, window.line, 20)
    style = window._trace_styles[window.label]
    style.line_enabled = False
    style.markers_enabled = True
    style.opacity = 0.55
    window._apply_trace_style(window.label, window.line)

    assert window._last_cursor_scene_pos == point
    assert window._hover_highlight.active_item is window.line
    _assert_highlighted_axes(window, ("top", "right"))
    assert window.line.scatter.graphicsEffect().isEnabled()
    assert window.line.curve.opts["pen"].style() == QtCore.Qt.PenStyle.NoPen
    assert window.line.graphicsEffect().opacity() == 0.55
    style.opacity = 1.0
    window._apply_trace_style(window.label, window.line)
    # Style changes schedule a cursor refresh after the renderer has updated.
    wait_for(lambda: window.line.scatter.graphicsEffect() is not None)
    assert window.line.graphicsEffect() is None
    assert window.line.scatter.graphicsEffect().isEnabled()
    assert window._hover_highlight.active_item is window.line


def test_stationary_cursor_refreshes_when_trace_data_changes(loaded_plot, no_callback_errors):
    window = loaded_plot
    window._create_axis_scale_dialog()
    window._apply_axis_scale_manual_limits("x", [-1, 65])
    window._apply_axis_scale_manual_limits("y", [0, 85])
    point = _move_to_sample(window, window.line, 20)
    x, y = (values.copy() for values in window.line.getOriginalDataset())
    window.line.setData(x=x + 0.2, y=y + 0.3)
    window.trace_updated.emit()

    assert window._last_cursor_scene_pos == point
    assert window._hover_highlight.active_item is window.line
    assert float(window.pos_labels["x"].text()) == pytest.approx(20.2)
    assert float(window.pos_labels["y"].text()) == pytest.approx(30.3)

    window.line.setData([], [])
    window.trace_updated.emit()
    assert window._hover_highlight.active_item is None
    _assert_highlighted_axes(window, ())
    assert window.snap_marker.scene() is None
    window.line.setData(x=x, y=y)
    window.trace_updated.emit()
    assert window._hover_highlight.active_item is window.line
    assert window.snap_marker.isVisible()


@pytest.mark.parametrize("loaded_plot", [True], indirect=True)
def test_removing_active_trace_releases_highlight(loaded_plot, no_callback_errors):
    window = loaded_plot
    key, line = next((key, line) for key, line in window.lines.items() if line is not window.line)
    owner = _assign_trace_axes(window, key, "Top", "Right")
    window.plot.vb.setRange(xRange=[-1, 65], yRange=[0, 160], padding=0)
    owner.setRange(xRange=[-1, 65], yRange=[-80, 80], padding=0)
    qtw.QApplication.processEvents()
    _move_to_sample(window, line, 20)
    assert window._hover_highlight.active_item is line

    window.remove_line(window._trace_display_label(key, line), key)

    assert key not in window.lines
    assert window._hover_highlight.active_item is None
    _assert_highlighted_axes(window, ())
    assert window.snap_marker.scene() is None
    assert line.scene() is None
    assert line.curve.graphicsEffect() is None
    assert line.scatter.graphicsEffect() is None


@pytest.mark.parametrize("loaded_plot", [True], indirect=True)
def test_stationary_cursor_tracks_secondary_source_publication(loaded_plot, no_callback_errors):
    window = loaded_plot
    key, line = next((key, line) for key, line in window.lines.items() if line is not window.line)
    owner = _assign_trace_axes(window, key, "Top", "Right")
    window.plot.vb.setRange(xRange=[-1, 65], yRange=[0, 160], padding=0)
    owner.setRange(xRange=[-1, 65], yRange=[-80, 80], padding=0)
    qtw.QApplication.processEvents()
    point = _move_to_sample(window, line, 20)
    assert window._hover_highlight.active_item is line

    source = line.from_win
    source.axis_data["y"] = source.axis_data["y"] + 0.3
    source.trace_updated.emit()
    wait_for(lambda: (
        window._hover_highlight.active_item is line
        and float(window.pos_labels["y"].text()) == pytest.approx(30.3)
    ))
    assert window._last_cursor_scene_pos == point
    assert window._hover_highlight.active_item is line
    assert float(window.pos_labels["y"].text()) == pytest.approx(30.3)

    source.axis_data["x"] = np.array([])
    source.axis_data["y"] = np.array([])
    source.trace_updated.emit()
    qtw.QApplication.processEvents()
    nearest = window._nearest_trace_point(point)
    assert nearest is None
    assert window._hover_highlight.active_item is None
    assert window.snap_marker.scene() is None
    _assert_highlighted_axes(window, ())
    free = window.line.getViewBox().mapSceneToView(point)
    assert window.pos_labels["x"].text() == window.format_cursor_number(free.x())
    assert window.pos_labels["y"].text() == window.format_cursor_number(free.y())


@pytest.mark.parametrize("mixed_plot", ["heatmap"], indirect=True)
def test_hiding_active_heatmap_removes_its_extent_and_colourbar_highlight(mixed_plot):
    _main, window, _runs = mixed_plot
    window.resize(1000, 700)
    window.show()
    qtw.QApplication.processEvents()
    point = window._primary_heatmap_viewbox().mapViewToScene(QtCore.QPointF(1, 10))
    window.plot.scene().sigMouseMoved.emit(point)
    assert window._hover_highlight.active_item is not None
    assert window._hover_highlight.outline.isVisible()
    assert window.bar.axis.graphicsEffect().isEnabled()

    window._set_layer_visibility(window, False)
    qtw.QApplication.processEvents()

    assert window._hover_highlight.active_item is None
    assert window._hover_highlight.outline is None
    assert window.bar.axis.graphicsEffect() is None


@pytest.mark.parametrize("mixed_plot", ["heatmap"], indirect=True)
def test_heatmap_hover_preserves_pixels_and_follows_axis_assignment(mixed_plot):
    _main, window, _runs = mixed_plot
    window.resize(1000, 700)
    window.show()
    original = window.dataGrid.copy()
    renderers = window._heatmap_render_items(window)
    renderer = next(item for item in renderers if item.isVisible())
    original_opacity = renderer.opacity()
    original_levels = window.bar.levels()
    for axes in [("Bottom", "Left"), ("Top", "Right")]:
        window._set_layer_axes(window, *axes)
        qtw.QApplication.processEvents()
        point = window._primary_heatmap_viewbox().mapViewToScene(QtCore.QPointF(1, 10))
        window.plot.scene().sigMouseMoved.emit(point)
        hover = window._hover_highlight
        assert hover.active_item is renderer
        _assert_highlighted_axes(window, tuple(side.lower() for side in axes))
        assert hover.outline is not None and hover.outline.isVisible()
        assert hover.outline not in window.plot.items
        assert hover.outline not in window.plot.listDataItems()
        assert window.bar.axis.graphicsEffect().isEnabled()
        assert window.pos_labels["index"].text().replace(" ", "") == "[1,0]"
        assert float(window.pos_labels["z"].text()) == 11
        assert renderer.opacity() == original_opacity
        assert window.bar.levels() == original_levels
        np.testing.assert_array_equal(window.dataGrid, original)


@pytest.mark.parametrize("mixed_plot", ["heatmap"], indirect=True)
@pytest.mark.parametrize("snapping", [False, True])
def test_empty_space_outside_heatmap_clears_axes_and_colour_scale(
    mixed_plot, monkeypatch, snapping,
):
    monkeypatch.setitem(pg.CONFIG_OPTIONS, "mouseRateLimit", 0)
    _main, window, _runs = mixed_plot
    window.snap_to_trace_action.setChecked(snapping)
    window.resize(1000, 700)
    window.show()
    owner = window._primary_heatmap_viewbox()
    owner.setRange(xRange=[-2, 4], yRange=[-5, 35], padding=0)
    qtw.QApplication.processEvents()
    inside = owner.mapViewToScene(QtCore.QPointF(1, 10))
    outside = owner.mapViewToScene(QtCore.QPointF(3.25, 15))
    assert window._cursor_inside_plot(outside)
    assert window.heatmap_sample_at(3.25, 15) is None

    for point in (inside, outside, inside):
        _viewport_move(window, point)
        if point == outside:
            assert window._hover_highlight.active_item is None
            _assert_highlighted_axes(window, ())
            assert window._hover_highlight.effects == ()
            assert window._hover_highlight.outline is None
            assert window.bar.axis.graphicsEffect() is None
            assert not window.hover_pixel_outline.isVisible()
            assert window.pos_labels["z"].text() == ""
        else:
            assert window._hover_highlight.active_item in window._heatmap_render_items(window)
            _assert_highlighted_axes(window, ("bottom", "left"))
            assert window._hover_highlight.outline.isVisible()
            assert window.bar.axis.graphicsEffect().isEnabled()
            assert window.hover_pixel_outline.isVisible()
            assert float(window.pos_labels["z"].text()) == 11


@pytest.mark.parametrize("mixed_plot", ["heatmap"], indirect=True)
def test_mixed_plot_highlight_switches_with_trace_and_heatmap_readouts(mixed_plot):
    _main, window, runs = mixed_plot
    _drop(window, runs["line"])
    line = _added(window, runs["line"])
    window.show()
    qtw.QApplication.processEvents()
    _move_to_sample(window, line, 1)
    assert window._hover_highlight.active_item is line
    assert window.pos_labels["z"].isHidden()

    window.snap_to_trace_action.setChecked(False)
    point = window._primary_heatmap_viewbox().mapViewToScene(QtCore.QPointF(1, 10))
    window.plot.scene().sigMouseMoved.emit(point)
    assert window._hover_highlight.active_item in window._heatmap_render_items(window)
    assert window._hover_highlight.outline.isVisible()
    assert not window.pos_labels["z"].isHidden()
    assert float(window.pos_labels["z"].text()) == 11
