"""Axis warning symbols follow labels without changing plot layout or data."""

from types import SimpleNamespace

import pyqtgraph as pg
import pytest
from PyQt6 import QtCore

from qplot.windows._plot_hover import PlotHoverHighlight
from tests.windows.test_native_transform_labels import (
    no_callback_errors as no_callback_errors,
)


@pytest.fixture
def warning_plot(qapplication):
    widget = pg.PlotWidget(background="w")
    widget.resize(700, 500)
    plot = widget.getPlotItem()
    for side in ("bottom", "left", "top", "right"):
        plot.showAxis(side)
        plot.setLabel(side, f"{side.title()} quantity", units="V")
    line = plot.plot([0, 1, 2], [1, 2, 1], pen="r", symbol="o")
    widget.show()
    qapplication.processEvents()
    hover = PlotHoverHighlight(SimpleNamespace(plot=plot))
    try:
        yield widget, plot, line, hover
    finally:
        hover.clear()
        widget.close()
        widget.deleteLater()


@pytest.mark.parametrize("side", ["bottom", "left", "top", "right"])
@pytest.mark.parametrize("background", ["w", "k"])
def test_warning_badge_tracks_label_changes_and_resizing(
    warning_plot, qapplication, no_callback_errors, side, background,
):
    widget, plot, line, hover = warning_plot
    widget.setBackground(background)
    assigned = (side, "left") if side in ("top", "bottom") else ("bottom", side)
    axis = plot.getAxis(side)
    original_label = axis.label.toHtml()
    original_bounds = line.dataBounds(0), line.dataBounds(1)
    original_items = plot.listDataItems()
    hover.set_trace(line, assigned, warning_axes=(side,))
    badge = hover.warning_badges[side]

    def assert_badge():
        assert badge.isVisible()
        assert badge.rotation() == 0
        assert badge.parentItem() is axis
        assert not badge.sceneBoundingRect().intersects(axis.label.sceneBoundingRect())
        expected = axis.label.mapToParent(QtCore.QPointF(
            axis.label.boundingRect().right() + 8,
            axis.label.boundingRect().center().y(),
        ))
        offset = badge.pos() + badge.boundingRect().center() - expected
        # The one-pixel viewport inset can nudge the outermost axis row.
        assert abs(offset.x()) <= 1
        assert abs(offset.y()) <= 1

    assert_badge()
    assert axis.label.toHtml() == original_label
    assert line.dataBounds(0) == original_bounds[0]
    assert line.dataBounds(1) == original_bounds[1]
    assert plot.listDataItems() == original_items
    assert badge.acceptedMouseButtons() == QtCore.Qt.MouseButton.NoButton
    assert not badge.acceptHoverEvents()
    assert badge.toolTip() == ""
    assert axis.graphicsEffect().color() != line.scatter.graphicsEffect().color()

    axis.setLabel("A longer measured quantity", units="A")
    qapplication.processEvents()
    assert_badge()
    previous_position = badge.scenePos()
    widget.resize(950, 650)
    qapplication.processEvents()
    assert_badge()
    assert badge.scenePos() != previous_position
    axis.label.hide()
    assert not badge.isVisible()
    axis.label.show()
    assert_badge()


@pytest.mark.parametrize("side", ["bottom", "left", "top", "right"])
def test_long_labels_keep_warning_symbol_in_viewport(
    warning_plot, qapplication, no_callback_errors, side,
):
    widget, plot, line, hover = warning_plot
    widget.resize(360, 300)
    axis = plot.getAxis(side)
    axis.setLabel("A deliberately very long description of the displayed measured quantity", units="A")
    qapplication.processEvents()
    original_label = axis.label.toHtml()
    assigned = (side, "left") if side in ("top", "bottom") else ("bottom", side)
    hover.set_trace(line, assigned, warning_axes=(side,))
    badge = hover.warning_badges[side]
    viewport = widget.mapToScene(widget.viewport().rect()).boundingRect()
    assert viewport.contains(badge.sceneBoundingRect())
    assert axis.label.toHtml() == original_label


def test_downward_label_keeps_warning_upright_and_moves_with_rotation(
    warning_plot, qapplication, no_callback_errors,
):
    _widget, plot, line, hover = warning_plot
    axis = plot.getAxis("right")
    hover.set_trace(line, ("bottom", "right"), warning_axes=("right",))
    badge = hover.warning_badges["right"]
    axis.label.setRotation(90)
    # Let the pending axis layout finish before positioning the rotated label.
    # Its default placement assumes the usual upward-facing right-hand label.
    qapplication.processEvents()
    bounds = axis.label.boundingRect()
    axis.label.setPos(axis.width() + 5, axis.height() / 2 - bounds.width() / 2)
    assert badge.rotation() == 0
    assert badge.sceneBoundingRect().top() > axis.label.sceneBoundingRect().bottom()
    with hover.suspended():
        axis.setLabel("Updated label", units="S")
        assert not badge.isVisible()
    assert badge.isVisible()
