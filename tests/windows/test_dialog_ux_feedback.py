"""Range corrections stay local to their editor and recover cleanly."""

import numpy as np
import pytest
from PyQt6 import QtCore, QtTest

from tests.support.windows.axis_limit_precision import AxisLimitWindow
from tests.support.windows.plot_dialog_lifecycle import dialog_plot as _dialog_plot

dialog_plot = _dialog_plot


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("held", [None, "span"])
def test_cancel_restores_complete_colour_scale(dialog_plot, manual, held):
    _window, plot, app, _guids = dialog_plot
    entry = plot.colorbar_range_entry
    if manual:
        plot.setColorbarManualRange(-4.0, 4.0)
    if held:
        entry.locks[held].click()
    entry.percent.setValue(85)
    before_state = entry.state
    before_levels = plot._current_colorbar_levels()
    before_map = plot._current_colorbar_colormap_name()
    before_default = plot.config.get("user_preference.bar_colour")
    plot.appearance_action.trigger()
    dialog = plot._plot_appearance_dialog
    dialog.show_page("Colour scale")
    plot.setColorbarColorMap("plasma")
    entry.locks["max"].click()
    entry.visible_only.click()
    entry.percent.setValue(50)
    if manual:
        entry.auto_radio.click()
    else:
        entry.manual_radio.click()
    entry.show_error("Invalid draft")
    dialog.reject()
    app.processEvents()
    assert plot._current_colorbar_colormap_name() == before_map
    np.testing.assert_allclose(plot._current_colorbar_levels(), before_levels)
    assert entry.state == before_state
    assert entry.percent.value() == 85
    assert not entry.visible_only.isChecked()
    assert not entry.error_label.text()
    assert plot.config.get("user_preference.bar_colour") == before_default


@pytest.mark.parametrize("dialog_plot", ["heatmap", "line"], indirect=True)
def test_empty_appearance_selection_is_safe_and_survives_refresh(dialog_plot):
    _window, plot, app, _guids = dialog_plot
    plot.appearance_action.trigger()
    dialog = plot._plot_appearance_dialog
    assert dialog.selected_group.isAncestorOf(dialog.selection_summary)
    before_styles = {key: vars(style).copy() for key, style in plot._trace_styles.items()}
    dialog.table.clearSelection()
    app.processEvents()
    assert not dialog._selected_entries()
    assert dialog.selection_summary.text() == "Select a dataset to edit its appearance."
    assert dialog.selection_summary.toolTip() == ""
    assert not dialog.visible.isEnabled()
    assert not dialog.heatmap_controls.visible.isEnabled()
    assert not dialog.remove_item_button.isEnabled()
    dialog.table.setCurrentCell(-1, -1)
    dialog.refresh_rows()
    assert not dialog._selected_entries()
    assert "Select a dataset" in dialog.selection_summary.text()
    assert {key: vars(style).copy() for key, style in plot._trace_styles.items()} == before_styles
    dialog.table.selectRow(0)
    app.processEvents()
    assert len(dialog._selected_entries()) == 1
    kind = "heatmap" if plot.operation_kind == "plot2d" else "trace"
    name = dialog.table.item(0, dialog._COL_MEASUREMENT).text()
    assert dialog.selection_summary.text() == f"<b>{name}</b> — {kind}"


def test_range_draft_survives_refresh_and_corrects_without_precision_loss(qapplication):
    window = AxisLimitWindow()
    window.plot.plot([1, 2, 3], [1, 2, 3])
    window.vb.updateAutoRange()
    window.open_axis_scale_dialog("x")
    entry = window._axis_scale_controls["x"].rangeEntry
    before = list(window.vb.viewRange()[0])
    field = entry.fields["min"]
    try:
        field.setFocus()
        field.selectAll()
        QtTest.QTest.keyClicks(field, "9")
        QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Return)
        window._sync_axis_scale_controls("x")
        assert field.text() == "9" and entry.error_label.isVisible()
        np.testing.assert_array_equal(window.vb.viewRange()[0], before)
        field.selectAll()
        QtTest.QTest.keyClicks(field, "1.0000000000000002")
        QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Return)
        assert not entry.error_label.isVisible()
        np.testing.assert_array_equal(window.vb.viewRange()[0], [1.0000000000000002, before[1]])
    finally:
        window._axis_scale_dialog.close()
        window.close()
        window.deleteLater()
