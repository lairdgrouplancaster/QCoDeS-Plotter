"""OK commits and Cancel restores the complete live appearance preview."""

import threading
from copy import deepcopy

import pytest
from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.windows._export_tasks import start_export_task
from qplot.windows._preferences import PreferencesDialog
from tests.support.windows.mixed_plot_drops import _added, _wait_for
from tests.support.windows.mixed_plot_drops import mixed_plot as _mixed_plot
from tests.support.windows.plot3d import build_mixed_volume, loaded
from tests.support.windows.plot3d import main as _volume_main
from tests.support.windows.preferences_value_preservation import (
    precise_config as _precise_config,
)

mixed_plot = _mixed_plot
precise_config = _precise_config
volume_main = _volume_main


def test_plot_settings_pages_preview_and_restore_axis_edits(mixed_plot):
    _window, plot, _runs = mixed_plot
    plot.open_axis_scale_dialog("x")
    editor = plot._axis_scale_dialog
    assert editor is plot._plot_appearance_dialog and editor.pages.currentWidget() is editor.axes_page
    controls = plot._axis_scale_controls["x"]
    initial = plot._capture_axis_settings("x")
    controls.invertCheck.click()
    controls.mouseCheck.click()
    controls.manualRadio.click()
    for field, value in ((controls.minText, "-6"), (controls.maxText, "10")):
        field.setFocus()
        field.selectAll()
        QtTest.QTest.keyClicks(field, value)
        QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Return)
    controls.rangeEntry.locks["max"].click()
    assert controls.rangeEntry.state.lock == "max"
    assert editor.isVisible()
    assert plot.vb.state["targetRange"][0] == [-6, 10]
    plot.open_plot_appearance_dialog()
    assert editor.pages.currentIndex() == 0
    editor.reject()
    restored = plot._capture_axis_settings("x")
    assert controls.rangeEntry.state.lock is None
    assert restored.mouse == initial.mouse and restored.inverted == initial.inverted
    assert restored.auto == initial.auto and restored.filtered_auto == initial.filtered_auto
    if initial.auto is False and not initial.filtered_auto:
        assert restored.limits == initial.limits
    plot.open_axis_scale_dialog("x")
    controls.invertCheck.click()
    keep = editor.appearance_buttons.button(qtw.QDialogButtonBox.StandardButton.Ok)
    keep.setFocus()
    QtTest.QTest.keyClick(keep, QtCore.Qt.Key.Key_Return)
    assert not editor.isVisible()
    assert plot._capture_axis_settings("x").inverted != initial.inverted


def test_keep_changes_returns_to_invalid_range(mixed_plot, qapplication):
    _main, plot, _runs = mixed_plot
    plot.open_axis_scale_dialog("x")
    dialog = plot._plot_appearance_dialog
    if plot.operation_kind == "plot1d":
        entry = plot._axis_scale_controls["x"].rangeEntry
    else:
        dialog.show_page("Colour scale")
        entry = plot.colorbar_range_entry
    dialog.activateWindow()
    qapplication.processEvents()
    keep = dialog.appearance_buttons.button(qtw.QDialogButtonBox.StandardButton.Ok)
    span = entry.fields["span"]
    initial = entry.state
    span.setFocus()
    span.selectAll()
    QtTest.QTest.keyClicks(span, "-1")
    QtTest.QTest.mouseClick(keep, QtCore.Qt.MouseButton.LeftButton)
    qapplication.processEvents()
    assert dialog.isVisible() and span.hasFocus() and span.selectedText() == "-1"
    assert entry.error_label.isVisible() and "greater than zero" in entry.error_label.text()
    assert entry.state.limits == initial.limits
    QtTest.QTest.keyClick(span, QtCore.Qt.Key.Key_Escape)
    assert entry.state.automatic == initial.automatic and not entry.error_label.isVisible()
    span.selectAll()
    QtTest.QTest.keyClicks(span, "oops")
    keep.setFocus()
    QtTest.QTest.keyClick(keep, QtCore.Qt.Key.Key_Return)
    assert dialog.isVisible() and span.selectedText() == "oops"
    keep.setFocus()
    entry.fields["max"].setFocus()
    assert entry.state.automatic == initial.automatic and span.text() != "oops"
    span.setFocus()
    span.selectAll()
    QtTest.QTest.keyClicks(span, "3")
    QtTest.QTest.mouseClick(keep, QtCore.Qt.MouseButton.LeftButton)
    assert not dialog.isVisible() and entry.state.value("span") == pytest.approx(3)
    dialog.show()
    dialog.activateWindow()
    qapplication.processEvents()
    span.setFocus()
    span.selectAll()
    QtTest.QTest.keyClicks(span, "-1")
    cancel = dialog.appearance_buttons.button(qtw.QDialogButtonBox.StandardButton.Cancel)
    QtTest.QTest.mouseClick(cancel, QtCore.Qt.MouseButton.LeftButton)
    assert not dialog.isVisible() and entry.state.value("span") == pytest.approx(3)


def test_mixed_selection_applies_shared_controls_and_reorders(mixed_plot):
    _window, plot, runs = mixed_plot
    primary = plot._plot_item_order[0]
    foreign = runs["heatmap" if plot.operation_kind == "plot1d" else "line"]
    plot.open_plot_appearance_dialog()
    editor = plot._plot_appearance_dialog
    add(editor, foreign, "heatmap" if plot.operation_kind == "plot1d" else "trace")
    _added(plot, foreign)
    before = tuple(plot._plot_item_order)
    table = editor.table
    table.setFocus()
    table.clearSelection()
    QtTest.QTest.keyClick(table, QtCore.Qt.Key.Key_Up, QtCore.Qt.KeyboardModifier.AltModifier)
    assert tuple(plot._plot_item_order) == before
    table.selectRow(1)
    for key, expected_order in (
        (QtCore.Qt.Key.Key_Down, before),
        (QtCore.Qt.Key.Key_Up, before[::-1]),
        (QtCore.Qt.Key.Key_Up, before[::-1]),
        (QtCore.Qt.Key.Key_Down, before),
    ):
        QtTest.QTest.keyClick(table, key, QtCore.Qt.KeyboardModifier.AltModifier)
        assert tuple(plot._plot_item_order) == expected_order
        assert editor._selected_entries() == [before[1]]
    table.selectAll()
    assert editor.controls_stack.currentWidget() is editor.trace_controls_panel
    assert "Line and marker styles affect 1 trace" in editor.selection_summary.text()
    assert editor.x_axis.parentWidget().title() == "Dataset axes"
    editor.opacity.setValue(43)
    assert all(style.opacity == .43 for style in plot._trace_styles.values())
    assert all(plot._heatmap_opacity(layer) == .43 for layer in plot.heatmaps.values())
    editor.visible.click()
    assert not any(style.visible for style in plot._trace_styles.values())
    assert not any(plot._heatmap_is_visible(layer) for layer in plot.heatmaps.values())
    for row in range(table.rowCount()):
        name = table.item(row, editor._COL_MEASUREMENT)
        assert name.data(QtCore.Qt.ItemDataRole.CheckStateRole) is None
        assert not (name.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable)
        assert name.font().strikeOut()
        assert name.data(QtCore.Qt.ItemDataRole.AccessibleDescriptionRole) == "Hidden"

    added_row = next(row for row in range(table.rowCount()) if editor._row_entry(row) != primary)
    name = table.item(added_row, editor._COL_MEASUREMENT)
    QtTest.QTest.mouseClick(table.viewport(), QtCore.Qt.MouseButton.LeftButton,
                            pos=table.visualItemRect(name).center())
    assert editor._selected_entries() == [editor._row_entry(added_row)]
    controls = editor if editor._row_entry(added_row)[0] == "trace" else editor.heatmap_controls
    assert controls.visible.isVisible() and not controls.visible.isChecked()
    controls.visible.click()
    for row in range(table.rowCount()):
        assert table.item(row, editor._COL_MEASUREMENT).font().strikeOut() == (row != added_row)
    editor.reject()
    assert list(plot._plot_item_order) == [primary]
    assert all(style.visible for style in plot._trace_styles.values())
    assert all(plot._heatmap_is_visible(layer) for layer in plot.heatmaps.values())


def add(dialog, run, kind):
    index = next(index for index in range(dialog.add_item_combo.count())
                 if (entry := dialog.add_item_combo.itemData(index)) is not None
                 and entry[0] == kind and entry[1].parameter_name == run.name)
    dialog.add_item_combo.setCurrentIndex(index)
    dialog.add_item_button.click()


@pytest.mark.parametrize("exit_action", ["cancel", "escape", "close"])
def test_cancel_restores_membership_order_styles_and_axes(mixed_plot, monkeypatch, exit_action):
    _main, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    before_order = list(target._plot_item_order)
    before_styles = deepcopy(target._trace_styles)
    before_axes = dict(target.axis_options)
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    if dialog.swap_axes.isEnabled():
        dialog.swap_axes.click()
    for name, kind in (("other_line", "trace"), ("other_heatmap", "heatmap")):
        add(dialog, runs[name], kind)
        _added(target, runs[name])
    dialog._move_rows_to_position([dialog.table.rowCount() - 1], 0)
    row = next(row for row in range(dialog.table.rowCount()) if dialog._row_entry(row) == before_order[0])
    dialog.table.selectRow(row)
    dialog.remove_item_button.click()
    assert dialog.table.rowCount() == 2
    if exit_action == "cancel":
        dialog.appearance_buttons.button(qtw.QDialogButtonBox.StandardButton.Cancel).click()
    elif exit_action == "escape":
        QtTest.QTest.keyClick(dialog, QtCore.Qt.Key.Key_Escape)
    else:
        dialog.close()
    assert not dialog.isVisible()
    assert dialog.result() == qtw.QDialog.DialogCode.Rejected
    assert target._plot_item_order == before_order
    assert target._trace_styles == before_styles
    assert target.axis_options == before_axes
    assert not target._closed
    _wait_for(lambda: not target.worker.running)


def test_ok_commits_added_and_removed_datasets(mixed_plot, monkeypatch):
    main, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    primary = target._plot_item_order[0]
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    add(dialog, runs["other_line"], "trace")
    _added(target, runs["other_line"])
    dialog.table.selectRow(next(row for row in range(dialog.table.rowCount()) if dialog._row_entry(row) == primary))
    dialog.remove_item_button.click()
    assert not target._closed
    dialog.appearance_buttons.button(qtw.QDialogButtonBox.StandardButton.Ok).click()
    replacement = main.windows[-1]
    _wait_for(lambda: not replacement.worker.running)
    assert target._closed
    assert len(replacement._plot_item_order) == 1
    assert replacement.param.name == runs["other_line"].name
    assert not replacement.__dict__.get("_plot_appearance_dialog")


def test_default_colour_map_is_a_staged_preference(precise_config):
    cfg = precise_config
    original = cfg.get("user_preference.bar_colour")
    dialog = PreferencesDialog(cfg)
    dialog.defaultColourMapButton.setValue("plasma")
    assert cfg.get("user_preference.bar_colour") == original
    dialog.reject()
    assert cfg.get("user_preference.bar_colour") == original
    dialog = PreferencesDialog(cfg)
    dialog.defaultColourMapButton.setValue("plasma")
    dialog.buttonBox.button(qtw.QDialogButtonBox.StandardButton.Ok).click()
    assert cfg.get("user_preference.bar_colour") == "plasma"


def test_cancel_prevents_deferred_addition_from_publishing(mixed_plot, monkeypatch):
    main, target, runs = mixed_plot
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    before = list(target._plot_item_order)
    resumes = []

    def defer(_target, resume, **_kwargs):
        resumes.append(resume)
        return True

    monkeypatch.setattr(main, "_defer_trusted_plot_action", defer)
    add(dialog, runs["other_line"], "trace")
    assert resumes
    dialog.reject()
    for resume in list(resumes):
        resume()
    assert len(resumes) == 1
    assert target._plot_item_order == before


def test_default_colour_preference_keeps_existing_plot_map(mixed_plot):
    main, target, runs = mixed_plot
    original = target._current_colorbar_colormap_name()
    preferences = PreferencesDialog(target.config)
    preferences.preferencesApplied.connect(main.apply_current_settings)
    preferences.defaultColourMapButton.setValue("plasma")
    assert preferences.defaultColourMapButton.text() == "Plasma"
    assert preferences.apply_preferences()
    assert target._current_colorbar_colormap_name() == original
    main.openPlot(runs["other_heatmap"].guid)
    _wait_for(lambda: main.windows[-1] is not target and not main.windows[-1].worker.running)
    assert main.windows[-1]._current_colorbar_colormap_name() == "plasma"
    preferences.close()


@pytest.mark.parametrize("commit", [False, True])
def test_volume_addition_is_a_cancellable_preview(volume_main, tmp_path, monkeypatch, commit):
    from qplot.windows._plot_drop_axes import DropAxisPlan

    window, errors = volume_main
    monkeypatch.setattr(DropAxisPlan, "approve", lambda *args: True)
    runs = build_mixed_volume(tmp_path / "appearance-volume.db")
    window.load_file(str(tmp_path / "appearance-volume.db"))
    _wait_for(lambda: not window._database_load_active)
    window.openPlot(runs[1][0])
    _wait_for(lambda: bool(window.windows) and loaded(window.windows[-1]))
    original = window.windows[-1]
    original_order = list(original._plot_item_order)
    original.open_plot_appearance_dialog()
    dialog = original._plot_appearance_dialog
    add(dialog, type("Volume", (), {"name": runs[3][1]})(), "heatmap")
    _wait_for(lambda: dialog.owner is not original)
    replacement = dialog.owner
    assert replacement is not original
    _wait_for(lambda: loaded(replacement))
    assert not original._closed
    assert not original.isVisible()
    release = threading.Event()
    retiring = original if commit else replacement
    task = start_export_task(retiring, title="Plot data", destination=tmp_path / "preview.csv",
                             writer=lambda *_args: release.wait(20))
    confirmations = []
    def confirm(_parent, _title, message, *, affirmative):
        confirmations.append((affirmative, message))
        return len(confirmations) == 2
    monkeypatch.setattr("qplot.windows._mixed_appearance.confirm_action", confirm)
    finish = dialog.accept if commit else dialog.close
    try:
        finish()
        assert dialog.isVisible() and not retiring._closed and not task.cancelled
        finish()
        assert task.cancelled and len(confirmations) == 2
        assert all("1 unfinished export." in message for _action, message in confirmations)
    finally:
        release.set()
        _wait_for(lambda: task.done)
    if commit:
        assert original._closed
        assert window.windows == [replacement]
    else:
        assert not original._closed
        assert original.isVisible()
        assert original._plot_item_order == original_order
        assert window.windows == [original]
        assert replacement._closed
    assert not dialog.isVisible()
    assert not errors


@pytest.mark.parametrize("mode", ["volume", "slice", "locked"])
def test_cancel_restores_volume_colour_scope(volume_main, tmp_path, mode):
    window, errors = volume_main
    runs = build_mixed_volume(tmp_path / "appearance-scope.db")
    window.load_file(str(tmp_path / "appearance-scope.db"))
    _wait_for(lambda: not window._database_load_active)
    window.openPlot(runs[3][0])
    _wait_for(lambda: bool(window.windows) and loaded(window.windows[-1]) and window.windows[-1]._range_complete)
    plot = window.windows[-1]
    plot.color_mode.setCurrentIndex(plot.color_mode.findData(mode))
    last_auto = plot._last_auto_mode
    plot.open_plot_appearance_dialog()
    dialog = plot._plot_appearance_dialog
    plot.open_colorbar_scale_dialog()
    plot.colorbar_scope.setCurrentIndex(plot.colorbar_scope.findData("slice" if mode != "slice" else "volume"))
    dialog.reject()
    assert plot._color_mode == mode
    assert plot._last_auto_mode == last_auto
    assert plot.color_mode.currentData() == mode
    assert plot.colorbar_scope.currentData() == last_auto
    assert not plot.colorbar_scale_dialog.isVisible()
    assert not errors
