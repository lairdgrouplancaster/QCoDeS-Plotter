"""Range corrections stay local to their editor and recover cleanly."""

import numpy as np
import pytest

from tests.windows.test_axis_limit_precision import AxisLimitWindow
from tests.windows.test_plot_dialog_lifecycle import dialog_plot as _dialog_plot

dialog_plot = _dialog_plot


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("symmetric", [False, True])
@pytest.mark.parametrize("refresh", [False, True])
def test_cancel_restores_complete_colour_scale(dialog_plot, manual, symmetric, refresh):
    _window, plot, app, _guids = dialog_plot
    plot.symmetric_color.setChecked(symmetric)
    plot.relevel_refresh.setChecked(refresh)
    if manual:
        plot.setColorbarManualRange(-4.0, 4.0)
    else:
        plot.setColorbarAuto()
    before_levels = plot._current_colorbar_levels()
    before_map = plot._current_colorbar_colormap_name()
    before_default = plot.config.get("user_preference.bar_colour")
    plot.appearance_action.trigger()
    dialog = plot._plot_appearance_dialog
    plot.setColorbarColorMap("plasma")
    plot.symmetric_color.setChecked(not symmetric)
    plot.relevel_refresh.setChecked(not refresh)
    if manual:
        plot.setColorbarAuto()
    else:
        plot.setColorbarManualRange(10.0, 20.0)
    plot.colorbar_min_text.setText("99")
    plot.colorbar_max_text.setText("1")
    plot.colorbar_range_entry.show_error("Invalid draft")
    dialog.reject()
    app.processEvents()
    assert plot._current_colorbar_colormap_name() == before_map
    np.testing.assert_array_equal(plot._current_colorbar_levels(), before_levels)
    assert (plot._colorbar_manual_levels is not None) == manual
    assert plot.colorbar_manual_radio.isChecked() == manual
    assert plot.colorbar_auto_radio.isChecked() != manual
    assert plot._colorbar_symmetric == symmetric
    assert plot.symmetric_color.isChecked() == symmetric
    assert plot.relevel_refresh.isChecked() == refresh
    assert plot.relevel_refresh.isEnabled() != manual
    assert not plot.colorbar_range_entry._error
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


@pytest.mark.parametrize("centre_span", [False, True])
def test_invalid_draft_blocks_format_switch_and_keeps_selection(qapplication, centre_span):
    window = AxisLimitWindow()
    window.plot.plot([1, 2, 3], [1, 2, 3])
    window.open_axis_scale_dialog("x")
    ui = window._axis_scale_controls["x"]
    entry = ui.rangeEntry
    try:
        if centre_span:
            entry.center_span_button.click()
        ui.minText.setText("9")
        ui.maxText.setText("-2" if centre_span else "2")
        window._axis_scale_range_text_changed("x")
        before = list(window.vb.viewRange()[0])
        target = entry.min_max_button if centre_span else entry.center_span_button
        target.click()
        assert entry.center_span == centre_span
        assert entry.center_span_button.isChecked() == centre_span
        assert entry.min_max_button.isChecked() != centre_span
        assert ui.minText.text() == "9"
        assert ui.maxText.text() == ("-2" if centre_span else "2")
        assert "before changing input format" in entry.summary.text()
        assert ("span" in entry.summary.text()) == centre_span
        np.testing.assert_array_equal(window.vb.viewRange()[0], before)
        ui.minText.setText("1")
        ui.maxText.setText("2")
        ui.minText.editingFinished.emit()
        target.click()
        assert entry.center_span != centre_span
        assert not entry._error
    finally:
        window._axis_scale_dialog.close()
        window.close()
        window.deleteLater()


def test_range_draft_survives_refresh_and_corrects_without_precision_loss(qapplication):
    window = AxisLimitWindow()
    window.plot.plot([1, 2, 3], [1, 2, 3])
    window.open_axis_scale_dialog("x")
    ui = window._axis_scale_controls["x"]
    before = list(window.vb.viewRange()[0])
    try:
        ui.minText.setText("9")
        ui.maxText.setText("2")
        window._axis_scale_range_text_changed("x")
        window._sync_axis_scale_controls("x")
        assert ui.minText.text() == "9"
        assert ui.rangeEntry._error
        np.testing.assert_array_equal(window.vb.viewRange()[0], before)
        ui.minText.setText(repr(ui.rangeEntry._limits[0]))
        ui.maxText.setText(repr(ui.rangeEntry._limits[1]))
        window._axis_scale_range_text_changed("x")
        assert not ui.rangeEntry._error
        ui.maxText.setText("2")
        ui.minText.setText("1.0000000000000002")
        window._axis_scale_range_text_changed("x")
        assert not ui.rangeEntry._error
        np.testing.assert_array_equal(window.vb.viewRange()[0], [1.0000000000000002, 2.0])
        ui.maxText.setText("0")
        window._axis_scale_range_text_changed("x")
        window._axis_scale_auto_clicked("x")
        assert not ui.rangeEntry._error
        assert float(ui.minText.text()) < float(ui.maxText.text())
    finally:
        window._axis_scale_dialog.close()
        window.close()
        window.deleteLater()
