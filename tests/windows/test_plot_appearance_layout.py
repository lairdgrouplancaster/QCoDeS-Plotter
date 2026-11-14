"""Responsive appearance panes and independent dataset/whole-plot scope."""

from copy import deepcopy

import numpy as np
import pytest
from PyQt6 import QtCore

from qplot.windows import _appearance_layout
from tests.windows.test_mixed_plot_drops import _added, _drop, _wait_for, mixed_plot

__all__ = ["mixed_plot"]


@pytest.mark.parametrize("screen_size", [(900, 700), (560, 500)])
@pytest.mark.parametrize("font_size", [9, 14])
def test_appearance_fits_screen_and_keeps_actions_accessible(
    mixed_plot, monkeypatch, qapplication, screen_size, font_size,
):
    _main, target, _runs = mixed_plot
    long_name = "measurement_with_a_name_that_is_longer_than_the_available_column"
    for method in ("_trace_measurement_name", "_heatmap_measurement_name"):
        monkeypatch.setattr(target, method, lambda *_args: long_name)
    target.setStyleSheet(target.styleSheet() + f"QWidget {{ font-size: {font_size}pt; }}")
    screen = QtCore.QRect(30, 40, *screen_size)
    monkeypatch.setattr(_appearance_layout, "available_appearance_geometry", lambda _dialog: screen)
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    qapplication.processEvents()
    assert screen.contains(dialog.frameGeometry()), (screen, dialog.frameGeometry(), dialog.minimumSizeHint())
    assert dialog.table.horizontalScrollBar().maximum() == 0
    measurement = dialog.table.item(0, dialog._COL_MEASUREMENT)
    assert long_name in measurement.toolTip()
    assert len({button.width() for button in dialog.appearance_buttons.buttons()}) == 1
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
    assert dialog.table.horizontalScrollBar().maximum() == 0
    selected = QtCore.QRect(dialog.selected_group.mapTo(dialog, QtCore.QPoint()), dialog.selected_group.size())
    whole = QtCore.QRect(dialog.whole_plot_group.mapTo(dialog, QtCore.QPoint()), dialog.whole_plot_group.size())
    datasets = dialog.appearance_splitter.widget(0)
    dataset_rect = QtCore.QRect(datasets.mapTo(dialog, QtCore.QPoint()), datasets.size())
    assert selected.left() == whole.left()
    assert selected.right() == whole.right()
    assert selected.bottom() < whole.top()
    assert dialog.rect().contains(whole)
    if dialog.appearance_splitter.orientation() == QtCore.Qt.Orientation.Horizontal:
        assert dataset_rect.bottom() >= whole.bottom()
        assert dataset_rect.right() < selected.left()
    else:
        assert dataset_rect.bottom() < selected.top()
    dialog.reject()


def test_global_settings_remain_available_across_selection_types(mixed_plot, qapplication):
    _main, target, runs = mixed_plot
    foreign = runs["heatmap" if target.operation_kind == "plot1d" else "line"]
    _drop(target, foreign)
    _added(target, foreign)
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    dialog.resize(1000, 800)
    button = dialog.heatmap_controls.color_scale_button
    assert dialog.whole_plot_group.isAncestorOf(button)
    assert dialog.whole_plot_group.isAncestorOf(dialog.swap_axes)
    assert not dialog.selected_group.isAncestorOf(button)
    for row in range(dialog.table.rowCount()):
        dialog.table.selectRow(row)
        qapplication.processEvents()
        def section_fits():
            controls = (dialog.heatmap_controls
                        if dialog.controls_stack.currentWidget() is dialog.heatmap_controls else dialog)
            axes = controls.x_axis.parentWidget()
            bottom = axes.mapTo(dialog.selected_group, axes.rect().bottomLeft()).y()
            gap = dialog.selected_group.rect().bottom() - bottom
            return not dialog.controls_scroll.verticalScrollBar().isVisible() and 0 <= gap <= 24
        _wait_for(section_fits)
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
    assert dialog.select_trace(key)
    # Settle native show/focus events before editing the selected dataset.
    qapplication.processEvents()
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
