"""OK commits and Cancel restores the complete live appearance preview."""

from copy import deepcopy

import pytest
from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.windows._preferences import PreferencesDialog
from tests.windows.test_mixed_plot_drops import _added, _wait_for
from tests.windows.test_mixed_plot_drops import mixed_plot as _mixed_plot
from tests.windows.test_plot3d import build_mixed_volume, loaded
from tests.windows.test_plot3d import main as _volume_main
from tests.windows.test_preferences_value_preservation import (
    precise_config as _precise_config,
)

mixed_plot = _mixed_plot
precise_config = _precise_config
volume_main = _volume_main


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
    dialog.defaultColourMapCombo.setCurrentIndex(dialog.defaultColourMapCombo.findData("plasma"))
    assert cfg.get("user_preference.bar_colour") == original
    dialog.reject()
    assert cfg.get("user_preference.bar_colour") == original
    dialog = PreferencesDialog(cfg)
    dialog.defaultColourMapCombo.setCurrentIndex(dialog.defaultColourMapCombo.findData("plasma"))
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
    preferences.defaultColourMapCombo.setCurrentIndex(preferences.defaultColourMapCombo.findData("plasma"))
    assert not preferences.defaultColourMapPreview.pixmap().isNull()
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
    if commit:
        dialog.accept()
        assert original._closed
        assert window.windows == [replacement]
    else:
        dialog.reject()
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
    assert plot.colorbar_scope.currentData() == mode
    assert not plot.colorbar_scale_dialog.isVisible()
    assert not errors
