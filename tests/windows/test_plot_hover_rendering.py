"""Hover effects preserve visible data when most of a trace is off screen."""

from types import SimpleNamespace

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore, QtGui, sip
from PyQt6 import QtWidgets as qtw

from qplot.windows._plot_hover import PlotHoverHighlight
from tests.windows.test_native_transform_labels import (
    no_callback_errors as no_callback_errors,
)


def _render(widget, pixel_ratio):
    image = QtGui.QImage(
        widget.width() * pixel_ratio, widget.height() * pixel_ratio,
        QtGui.QImage.Format.Format_RGBA8888,
    )
    image.setDevicePixelRatio(pixel_ratio)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    try:
        qtw.QWidget.render(widget, painter)
    finally:
        painter.end()
    return np.frombuffer(
        image.constBits().asstring(image.sizeInBytes()), dtype=np.uint8,
    ).reshape(image.height(), image.width(), 4).copy()


@pytest.mark.parametrize("background", ["w", "k"])
@pytest.mark.parametrize("pixel_ratio", [1, 2])
@pytest.mark.parametrize("scatter", [False, True])
def test_extreme_zoom_preserves_trace_pixels_and_bounds_temporary_rendering(
    qapplication, no_callback_errors, background, pixel_ratio, scatter,
):
    widget = pg.PlotWidget(background=background)
    plot = widget.getPlotItem()
    line = plot.plot(
        [-1e8, -0.6, 0, 0.6, 1e8], [0.5] * 5,
        pen=None if scatter else pg.mkPen("r", width=2),
        symbol="o" if scatter else None, symbolSize=8,
        symbolPen="r", symbolBrush="r",
    )
    plot.setRange(xRange=(-1, 1), yRange=(0, 1), padding=0)
    hover = PlotHoverHighlight(SimpleNamespace(plot=plot))
    messages = []
    previous_handler = QtCore.qInstallMessageHandler(
        lambda _kind, _context, message: messages.append(message)
    )
    try:
        widget.show()
        # Resizing while highlighted exercises effect-cache invalidation too.
        for width in (640, 320, 800):
            widget.resize(width, 360)
            qapplication.processEvents()
            with hover.suspended():
                before = _render(widget, pixel_ratio)
            hover.set_trace(line, ("bottom", "left"))
            after = _render(widget, pixel_ratio)

            source = (before[:, :, 0] > 230) & (before[:, :, 1] < 30) & (before[:, :, 2] < 30)
            if scatter:
                # The symbol atlas is resampled when the target DPI differs
                # from the widget. Check opaque marker centres, not their
                # antialiased edges, where the halo is intentionally blended.
                source &= (
                    np.roll(source, 2 * pixel_ratio, axis=0) & np.roll(source, -2 * pixel_ratio, axis=0)
                    & np.roll(source, 2 * pixel_ratio, axis=1) & np.roll(source, -2 * pixel_ratio, axis=1)
                )
            assert np.count_nonzero(source) > 20
            if scatter:
                np.testing.assert_array_equal(after[source], before[source])
            highlighted_source = (after[:, :, 0] > 230) & (after[:, :, 1] < 30) & (after[:, :, 2] < 30)
            # Antialiasing and the axis halo can blend a few boundary pixels.
            # The actual red data must still occupy the same screen positions.
            assert np.count_nonzero(highlighted_source & source) >= 0.98 * np.count_nonzero(source)
            # All three visible markers/trace sections acquire a halo.
            for x in (-0.6, 0, 0.6):
                scene_point = plot.vb.mapViewToScene(QtCore.QPointF(x, 0.5))
                point = widget.viewport().mapTo(widget, widget.mapFromScene(scene_point))
                column, row = point.x() * pixel_ratio, point.y() * pixel_ratio
                radius = 14 * pixel_ratio
                region = np.s_[row-radius:row+radius, column-radius:column+radius]
                assert np.count_nonzero(before[region] != after[region]) > 10
        assert not any("QPainter" in message or "Paint device" in message for message in messages)
    finally:
        QtCore.qInstallMessageHandler(previous_handler)
        hover.clear()
        widget.close()
        widget.deleteLater()


@pytest.mark.parametrize("change", ["hide", "delete"])
def test_disappearing_heatmap_releases_axes_without_pointer_movement(
    qapplication, no_callback_errors, change,
):
    widget = pg.PlotWidget()
    plot = widget.getPlotItem()
    image = pg.ImageItem(np.arange(12).reshape(3, 4))
    plot.addItem(image)
    hover = PlotHoverHighlight(SimpleNamespace(plot=plot))
    try:
        hover.set_heatmap(image, ("bottom", "left"))
        assert hover.outline is not None
        if change == "hide":
            image.hide()
        else:
            sip.delete(image)
        assert hover.active_item is None
        assert hover.outline is None
        assert not hover.effects
        assert plot.getAxis("bottom").graphicsEffect() is None
        assert plot.getAxis("left").graphicsEffect() is None
    finally:
        hover.clear()
        widget.close()
        widget.deleteLater()


def test_same_trace_restores_effects_deleted_during_style_changes(qapplication):
    widget = pg.PlotWidget()
    plot = widget.getPlotItem()
    line = plot.plot([0, 1], [0, 1], symbol="o")
    hover = PlotHoverHighlight(SimpleNamespace(plot=plot))
    try:
        hover.set_trace(line, ("bottom", "left"))
        effect = line.scatter.graphicsEffect()
        line.scatter.setGraphicsEffect(None)
        assert sip.isdeleted(effect)
        hover.set_trace(line, ("bottom", "left"))
        assert line.scatter.graphicsEffect().isEnabled()
        assert hover.active_item is line
    finally:
        hover.clear()
        widget.close()
        widget.deleteLater()
