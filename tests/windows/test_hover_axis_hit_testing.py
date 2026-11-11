"""Moving onto plot decoration must not inspect data or highlight its axes."""

import pyqtgraph as pg
import pytest
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

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


def _viewport_move(window, scene_point, *, expect_scene_signal=True):
    pixel = window.widget.mapFromScene(scene_point)
    viewport = window.widget.viewport()
    assert viewport.rect().contains(pixel)
    delivered = []
    scene = window.plot.scene()
    scene.sigMouseMoved.connect(delivered.append)
    try:
        event = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseMove,
            QtCore.QPointF(pixel),
            QtCore.QPointF(viewport.mapToGlobal(pixel)),
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        qtw.QApplication.sendEvent(viewport, event)
        qtw.QApplication.processEvents()
    finally:
        scene.sigMouseMoved.disconnect(delivered.append)
    if expect_scene_signal:
        assert delivered, "The viewport event must reach the plot's actual mouse handler"
        return delivered[-1]
    assert not delivered, "The scene must discard this event to exercise unthrottled clearing"
    return window.widget.mapToScene(pixel)


def _axis_points(axis):
    # Use the rendered spine/tick geometry, including qPlot's one-pixel flush
    # adjustment and its inward ticks, rather than guessed layout margins.
    image = QtGui.QImage(8, 8, QtGui.QImage.Format.Format_ARGB32)
    painter = QtGui.QPainter(image)
    try:
        spine, ticks, texts = axis.generateDrawSpecs(painter)
    finally:
        painter.end()
    _pen, start, end = spine
    centre = (QtCore.QPointF(start) + QtCore.QPointF(end)) / 2
    result = {"spine": axis.mapToScene(centre)}
    if ticks:
        _pen, tick_start, tick_end = min(
            ticks,
            key=lambda tick: (QtCore.QPointF(tick[1]) - centre).manhattanLength(),
        )
        result["tick"] = axis.mapToScene((QtCore.QPointF(tick_start) + QtCore.QPointF(tick_end)) / 2)
    if texts:
        rect, _flags, _text = min(texts, key=lambda text: (text[0].center() - centre).manhattanLength())
        result["tick label"] = axis.mapToScene(rect.center())
    if axis.label.isVisible() and axis.labelText:
        result["axis label"] = axis.label.mapToScene(axis.label.boundingRect().center())
    return result


def _hover_state(window):
    highlight = window._hover_highlight
    return highlight.active_item, highlight.axis_sides, tuple(highlight.warning_badges)


@pytest.mark.parametrize("loaded_plot", [True], indirect=True)
@pytest.mark.parametrize("after_data_hover", [False, True])
def test_four_axes_do_not_activate_highlights_from_real_mouse_events(
    loaded_plot, no_callback_errors, monkeypatch, after_data_hover,
):
    # Every synthetic move is deliberate; do not let PyQtGraph drop events
    # because the test runs faster than a person's hand.
    monkeypatch.setitem(pg.CONFIG_OPTIONS, "mouseRateLimit", 0)
    window = loaded_plot
    secondary_key = next(key for key, line in window.lines.items() if line is not window.line)
    _assign_trace_axes(window, secondary_key, "Top", "Right")
    window.resize(1100, 800)
    window.plot.vb.setRange(xRange=[-5, 70], yRange=[0, 85], padding=0)
    qtw.QApplication.processEvents()
    window.widget.grab()  # Finish the ordinary axis layout and paint cache.
    points = {
        f"{side} {part}": point
        for side in ("bottom", "left", "top", "right")
        for part, point in _axis_points(window.plot.getAxis(side)).items()
    }
    assert len(points) == 16
    rect = window.plot.vb.rect()
    for side, point in {
        "left": QtCore.QPointF(rect.left(), rect.center().y()),
        "right": QtCore.QPointF(rect.right(), rect.center().y()),
        "top": QtCore.QPointF(rect.center().x(), rect.top()),
        "bottom": QtCore.QPointF(rect.center().x(), rect.bottom()),
    }.items():
        points[f"{side} view edge"] = window.plot.vb.mapToScene(point)
    data_point = window.line.getViewBox().mapViewToScene(QtCore.QPointF(31, 41))
    failures = []
    for name, point in points.items():
        window._clear_cursor_hover()
        if after_data_hover:
            _viewport_move(window, data_point)
            assert window._hover_highlight.active_item is window.line
        actual = _viewport_move(window, point)
        state = _hover_state(window)
        if state != (None, (), ()):
            failures.append((name, actual, state))
        _viewport_move(window, data_point)
        assert window._hover_highlight.active_item is window.line
    assert not failures, failures


def test_throttled_move_onto_axis_label_clears_hover_immediately(
    loaded_plot, no_callback_errors, monkeypatch,
):
    window = loaded_plot
    window.resize(1000, 700)
    qtw.QApplication.processEvents()
    axis = window.plot.getAxis("bottom")
    axis_label = axis.label.mapToScene(axis.label.boundingRect().center())
    point = window.line.getViewBox().mapViewToScene(QtCore.QPointF(31, 41))
    scene = window.plot.scene()
    monkeypatch.setattr(scene, "_moveEventIsAllowed", lambda: True)
    _viewport_move(window, point)
    assert window._hover_highlight.active_item is window.line
    monkeypatch.setattr(scene, "_moveEventIsAllowed", lambda: False)

    _viewport_move(window, axis_label, expect_scene_signal=False)

    assert _hover_state(window) == (None, (), ())
    assert window.snap_marker.scene() is None
    monkeypatch.setattr(scene, "_moveEventIsAllowed", lambda: True)
    _viewport_move(window, point)
    assert window._hover_highlight.active_item is window.line
    assert window.snap_marker.scene() is scene


@pytest.mark.parametrize("mixed_plot", ["heatmap"], indirect=True)
@pytest.mark.parametrize("after_data_hover", [False, True])
def test_colourbar_does_not_activate_data_highlights(mixed_plot, monkeypatch, after_data_hover):
    monkeypatch.setitem(pg.CONFIG_OPTIONS, "mouseRateLimit", 0)
    _main, window, _runs = mixed_plot
    window.resize(1100, 800)
    window.show()
    qtw.QApplication.processEvents()
    window.widget.grab()
    points = _axis_points(window.bar.axis)
    points["colour swatch"] = window.bar.bar.mapToScene(window.bar.bar.boundingRect().center())
    for side in ("bottom", "left", "top", "right"):
        axis = window.bar.getAxis(side)
        if axis.label.isVisible() and axis.labelText:
            points["colour scale label"] = axis.label.mapToScene(axis.label.boundingRect().center())
    data_point = window._primary_heatmap_viewbox().mapViewToScene(QtCore.QPointF(1, 10))
    failures = []
    for name, point in points.items():
        window._clear_cursor_hover()
        if after_data_hover:
            _viewport_move(window, data_point)
            assert window._hover_highlight.active_item in window._heatmap_render_items(window)
        actual = _viewport_move(window, point)
        state = _hover_state(window)
        if state != (None, (), ()):
            failures.append((name, actual, state))
        _viewport_move(window, data_point)
        assert window._hover_highlight.active_item in window._heatmap_render_items(window)
    assert not failures, failures
