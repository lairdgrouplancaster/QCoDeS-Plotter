"""The map-only chooser previews safely within the Appearance session."""

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.windows._colour_map_picker import ColourMapDialog, ColourMapPicker
from tests.windows.test_color_scale_locale import heatmap
from tests.windows.test_mixed_plot_drops import _added, _drop
from tests.windows.test_mixed_plot_drops import mixed_plot as _mixed_plot
from tests.windows.test_plot_dialog_lifecycle import dialog_plot

mixed_plot = _mixed_plot

__all__ = ["heatmap", "dialog_plot", "mixed_plot"]


def select_map(picker, name, app):
    picker.search.setText(name)
    row = picker.row_for_name(name)
    picker.table.scrollToItem(picker.table.item(row, 0))
    app.processEvents()
    QtTest.QTest.mouseClick(
        picker.table.viewport(), QtCore.Qt.MouseButton.LeftButton,
        pos=picker.table.visualItemRect(picker.table.item(row, 0)).center(),
    )
    app.processEvents()


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("exit_action", ["ok", "cancel", "escape", "close"])
def test_map_preview_and_exit_preserve_scaling(heatmap, qapplication, manual, exit_action):
    heatmap.symmetric_color.setChecked(True)
    heatmap.relevel_refresh.setChecked(False)
    if manual:
        assert heatmap.setColorbarManualRange(-1.2345678901234567, 2.345678901234567)
    before_levels = heatmap._current_colorbar_levels()
    before_manual = heatmap._colorbar_manual_levels
    dialog = ColourMapDialog(heatmap, heatmap)
    dialog.open()
    try:
        assert not dialog.findChildren(qtw.QRadioButton)
        assert not dialog.findChildren(qtw.QCheckBox)
        assert type(dialog.picker) is type(heatmap.colorbar_map_picker) is ColourMapPicker
        assert dialog.picker.current_name.text() == "Viridis"
        select_map(dialog.picker, "plasma", qapplication)
        assert heatmap._current_colorbar_colormap_name() == "plasma"
        assert heatmap.colorbar_current_name.text() == "Plasma"
        assert dialog.picker.current_name.text() == "Plasma"
        np.testing.assert_array_equal(heatmap.image.getColorMap().color, heatmap.bar.colorMap().color)
        if exit_action in ("ok", "cancel"):
            button = (qtw.QDialogButtonBox.StandardButton.Ok if exit_action == "ok"
                      else qtw.QDialogButtonBox.StandardButton.Cancel)
            dialog.buttons.button(button).click()
        elif exit_action == "escape":
            QtTest.QTest.keyClick(dialog, QtCore.Qt.Key.Key_Escape)
        else:
            dialog.close()
        expected = "plasma" if exit_action == "ok" else "viridis"
        assert not dialog.isVisible()
        assert heatmap._current_colorbar_colormap_name() == expected
        assert heatmap._colorbar_manual_levels == before_manual
        np.testing.assert_array_equal(heatmap._current_colorbar_levels(), before_levels)
        assert heatmap.symmetric_color.isChecked()
        assert not heatmap.relevel_refresh.isChecked()
        assert heatmap.relevel_refresh.isEnabled() != manual
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_preview_gradients_fill_resized_cells_without_selection_tint(heatmap, qapplication):
    dialog = ColourMapDialog(heatmap, heatmap)
    dialog.open()
    try:
        picker = dialog.picker
        picker.search.setText("viridis")
        widths = []
        for size in ((600, 390), (960, 520), (600, 390)):
            dialog.resize(*size)
            qapplication.processEvents()
            table = picker.table
            row = picker.row_for_name("viridis")
            rect = table.visualItemRect(table.item(row, 1))
            widths.append(rect.width())
            reference = pg.colormap.get("viridis").getLookupTable(nPts=rect.width() - 12, alpha=True)
            for selected in (False, True):
                table.selectRow(row) if selected else table.clearSelection()
                rendered = table.viewport().grab().toImage()
                ratio = rendered.devicePixelRatio()
                for fraction in (0.05, 0.5, 0.95):
                    offset = int((len(reference) - 1) * fraction)
                    pixel = rendered.pixelColor(
                        round((rect.left() + 6 + offset) * ratio), round(rect.center().y() * ratio),
                    )
                    actual = np.array(pixel.getRgb())
                    np.testing.assert_allclose(actual, reference[offset], atol=1)
        assert widths[1] > widths[0] + 250
        assert widths[2] == widths[0]
    finally:
        dialog.reject()
        dialog.deleteLater()


@pytest.mark.parametrize("keep_parent", [False, True])
def test_parent_appearance_owns_accepted_map_changes(dialog_plot, keep_parent):
    _window, plot, app, _guids = dialog_plot
    before = plot._current_colorbar_colormap_name()
    default = plot.config.get("user_preference.bar_colour")
    plot.open_plot_appearance_dialog()
    appearance = plot._plot_appearance_dialog
    controls = appearance.heatmap_controls
    controls.color_scale_button.click()
    dialog = controls.colour_map_dialog
    assert dialog.isVisible()
    assert not plot.__dict__.get("colorbar_scale_dialog")
    select_map(dialog.picker, "plasma", app)
    assert controls.color_scale_button.text() == "Plasma"
    dialog.buttons.button(qtw.QDialogButtonBox.StandardButton.Ok).click()
    assert plot._current_colorbar_colormap_name() == "plasma"
    # A new visit cancels back to the map accepted in the previous visit.
    controls.color_scale_button.click()
    second = controls.colour_map_dialog
    select_map(second.picker, "magma", app)
    second.reject()
    assert controls.color_scale_button.text() == "Plasma"
    if keep_parent:
        appearance.accept()
    else:
        appearance.reject()
    assert plot._current_colorbar_colormap_name() == ("plasma" if keep_parent else before)
    assert plot.config.get("user_preference.bar_colour") == default


@pytest.mark.parametrize("exit_action", ["parent_cancel", "plot_close"])
def test_closing_parent_also_cancels_open_map_preview(dialog_plot, exit_action):
    _window, plot, app, _guids = dialog_plot
    before = plot._current_colorbar_colormap_name()
    plot.open_plot_appearance_dialog()
    appearance = plot._plot_appearance_dialog
    appearance.heatmap_controls.color_scale_button.click()
    dialog = appearance.heatmap_controls.colour_map_dialog
    select_map(dialog.picker, "plasma", app)
    if exit_action == "parent_cancel":
        appearance.reject()
    else:
        plot.close()
    app.processEvents()
    assert not dialog.isVisible()
    assert not appearance.isVisible()
    assert plot._current_colorbar_colormap_name() == before


def test_whole_plot_picker_opens_with_a_trace_selected(mixed_plot, qapplication):
    _window, plot, runs = mixed_plot
    kind = "heatmap" if plot.operation_kind == "plot1d" else "line"
    _drop(plot, runs[kind])
    _added(plot, runs[kind])
    before = plot._current_colorbar_colormap_name()
    plot.open_plot_appearance_dialog()
    appearance = plot._plot_appearance_dialog
    appearance.select_trace(next(iter(plot.lines)))
    qapplication.processEvents()
    controls = appearance.heatmap_controls
    assert not controls.isVisible()
    assert controls.color_scale_button.isVisible()
    controls.color_scale_button.click()
    chooser = controls.colour_map_dialog
    assert chooser.isVisible()
    select_map(chooser.picker, "plasma", qapplication)
    assert controls.color_scale_button.text() == "Plasma"
    chooser.reject()
    assert plot._current_colorbar_colormap_name() == before
    appearance.reject()
