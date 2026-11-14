"""Responsive appearance panes and independent dataset/whole-plot scope."""

from copy import deepcopy

import numpy as np
import pytest
from PyQt6 import QtCore

from qplot.windows import _appearance_layout
from tests.windows.test_mixed_plot_drops import _added, _drop, mixed_plot

__all__ = ["mixed_plot"]


@pytest.mark.parametrize("screen_size", [(900, 700), (560, 500)])
@pytest.mark.parametrize("font_size", [9, 14])
def test_appearance_fits_screen_and_keeps_actions_accessible(
    mixed_plot, monkeypatch, qapplication, screen_size, font_size,
):
    _main, target, _runs = mixed_plot
    target.setStyleSheet(target.styleSheet() + f"QWidget {{ font-size: {font_size}pt; }}")
    screen = QtCore.QRect(30, 40, *screen_size)
    monkeypatch.setattr(_appearance_layout, "available_appearance_geometry", lambda _dialog: screen)
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    qapplication.processEvents()
    assert screen.contains(dialog.frameGeometry()), (screen, dialog.frameGeometry(), dialog.minimumSizeHint())
    for button in dialog.appearance_buttons.buttons():
        rect = QtCore.QRect(button.mapTo(dialog, QtCore.QPoint()), button.size())
        assert dialog.rect().contains(rect)
    if screen_size[0] == 560:
        assert dialog.appearance_splitter.orientation() == QtCore.Qt.Orientation.Vertical
    else:
        dialog.resize(1200, dialog.height())
        qapplication.processEvents()
        assert dialog.appearance_splitter.orientation() == QtCore.Qt.Orientation.Horizontal
    assert not dialog.controls_scroll.horizontalScrollBar().isVisible()
    assert dialog.controls_stack.width() <= dialog.controls_scroll.viewport().width()
    dialog.reject()


def test_global_settings_remain_available_across_selection_types(mixed_plot, qapplication):
    _main, target, runs = mixed_plot
    foreign = runs["heatmap" if target.operation_kind == "plot1d" else "line"]
    _drop(target, foreign)
    _added(target, foreign)
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    button = dialog.heatmap_controls.color_scale_button
    assert dialog.whole_plot_group.isAncestorOf(button)
    assert dialog.whole_plot_group.isAncestorOf(dialog.swap_axes)
    assert not dialog.selected_group.isAncestorOf(button)
    for row in range(dialog.table.rowCount()):
        dialog.table.selectRow(row)
        qapplication.processEvents()
        assert button.isVisible() and button.isEnabled()
    dialog.table.clearSelection()
    qapplication.processEvents()
    assert button.isVisible() and button.isEnabled()
    assert not dialog.line_enable.isEnabled()
    assert not dialog.heatmap_controls.opacity.isEnabled()
    before = target._current_colorbar_colormap_name()
    button.click()
    chooser = dialog.heatmap_controls.colour_map_dialog
    qapplication.processEvents()
    assert chooser.isVisible()
    chooser.picker.table.selectRow(chooser.picker.row_for_name("plasma"))
    chooser.accept()
    assert target._current_colorbar_colormap_name() == "plasma"
    dialog.reject()
    assert target._current_colorbar_colormap_name() == before


def test_one_marker_editor_changes_symbols_without_changing_samples(mixed_plot, qapplication):
    _main, target, runs = mixed_plot
    if target.operation_kind != "plot1d":
        _drop(target, runs["line"])
        _added(target, runs["line"])
    key, line = next(iter(target.lines.items()))
    data = tuple(np.array(values, copy=True) for values in line.getData())
    style = deepcopy(target._trace_styles[key])
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    dialog.select_trace(key)
    assert not hasattr(dialog, "dots_enable")
    dialog.marker_enable.setChecked(True)
    dialog.marker_symbol.setCurrentIndex(dialog.marker_symbol.findData("s"))
    dialog.marker_size.setValue(6.5)
    qapplication.processEvents()
    assert line.opts["symbol"] == "s"
    assert line.opts["symbolSize"] == 6.5
    for expected, actual in zip(data, line.getData(), strict=True):
        np.testing.assert_array_equal(expected, actual)
    dialog.reject()
    assert target._trace_styles[key] == style
