"""A plot CSV contains precisely the chosen measurement's loaded samples."""

import csv
import threading
from pathlib import Path

import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw

from qplot.windows._export_tasks import start_export_task
from qplot.windows._plot_export import CSV_TRANSFORMED_VALUES
from tests.support.windows.mixed_plot_drops import _added, _drop, _wait_for
from tests.support.windows.mixed_plot_drops import mixed_plot as mixed_plot


def _dialog(target):
    target.open_export_dialog()
    dialog = target.widget.scene().exportDialog
    dialog.controls.output_tabs.setCurrentIndex(1)
    qtw.QApplication.processEvents()
    return dialog


@pytest.mark.parametrize("scope", ["plot", "plots", "plots with routine confirmation", "database"])
def test_closing_warns_only_for_affected_unfinished_exports(mixed_plot, tmp_path, monkeypatch, scope):
    main, plot, _runs = mixed_plot
    entered, release = threading.Event(), threading.Event()
    confirmations = []
    replies = iter([False, True])

    def confirm(parent, _title, message, *, affirmative):
        confirmations.append((parent, affirmative, message))
        return next(replies)

    if scope == "plots with routine confirmation":
        scope = "plots"
        main.config.config["user_preference"]["confirm_close_all"] = True
        def answer_routine(box):
            yes, no = qtw.QMessageBox.StandardButton.Yes, qtw.QMessageBox.StandardButton.No
            assert box.defaultButton() is box.button(no)
            return yes if confirm(box.parent(), box.windowTitle(), box.informativeText(),
                                  affirmative=box.button(yes).text()) else no
        monkeypatch.setattr(qtw.QMessageBox, "exec", answer_routine)

    def write_plot(filename, *_args):
        entered.set()
        assert release.wait(20)
        return Path(filename).write_text("plot")

    monkeypatch.setattr("qplot.windows.main.confirm_action", confirm)
    monkeypatch.setattr("qplot.windows._plotWin.confirm_action", confirm)
    plot_task = start_export_task(plot, title="Plot data", destination=tmp_path / "plot.csv", writer=write_plot)
    raw_task = start_export_task(main, title="Raw data", destination=tmp_path / "raw.csv",
                                 writer=lambda name, *_args: Path(name).write_text("raw"))
    close = {"plot": plot.close, "plots": main.closeAll, "database": main.close_current_database}[scope]
    parent = plot if scope == "plot" else main
    affirmative = {"plot": "Close plot", "plots": "Close 1 plot", "database": "Close database"}[scope]
    expected_count = "2 unfinished exports" if scope == "database" else "1 unfinished export."
    try:
        _wait_for(entered.is_set)
        close()
        assert plot in main.windows and not plot_task.cancelled and not raw_task.cancelled
        assert plot.isVisible() and main.fileTextbox.text()
        close()
        assert plot not in main.windows and plot_task.cancelled
        assert raw_task.cancelled == (scope == "database")
        assert bool(main.fileTextbox.text()) == (scope != "database")
        assert len(confirmations) == 2
        assert all(owner is parent and action == affirmative and expected_count in message
                   for owner, action, message in confirmations)
    finally:
        release.set()
        _wait_for(lambda: plot_task.done and raw_task.done)
    assert raw_task.stage.published == (scope != "database")
    assert not plot_task.stage.published


def _select(dialog, item):
    exporter = dialog.currentExporter
    token = next(token for token, entry in exporter._qplot_csv_measurements.items() if entry.item is item)
    measurements = dialog.controls.fields["measurements"]
    measurements.setCurrentIndex(measurements.findData(token))
    qtw.QApplication.processEvents()
    assert dialog.currentExporter is exporter
    return dialog.controls.fields["measurements"].currentText()


def _save(target, dialog, path, monkeypatch):
    choices = []
    def destination(*args, **kwargs):
        choices.append(args)
        return str(path), "CSV files (*.csv)"
    monkeypatch.setattr(qtw.QFileDialog, "getSaveFileName", destination)
    dialog.ui.exportBtn.click()
    _wait_for(lambda: not target._export_tasks)
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.reader(stream))
    return rows[0], np.asarray(rows[1:], dtype=float), choices


@pytest.mark.parametrize("transformed", [False, True])
def test_select_trace_in_line_or_heatmap_plot(mixed_plot, tmp_path, monkeypatch, transformed):
    _window, target, runs = mixed_plot
    _drop(target, runs["other_line"])
    line = _added(target, runs["other_line"])
    line.setData(x=[4., 5., 6.], y=[40., 50., 60.])
    line.setDerivativeMode(True)
    dialog = _dialog(target)
    label = _select(dialog, line)
    assert f"Run {line.from_win.ds.run_id}" in label and "w" in label and "trace" in label
    assert "columnMode" not in dialog.controls.fields
    assert dialog.currentExporter.params.child("measurements").opts["title"] == "Data to export"
    values = dialog.controls.fields["values"]
    if transformed:
        values.setCurrentIndex(values.findData(CSV_TRANSFORMED_VALUES))
    headers, data, choices = _save(target, dialog, tmp_path / "trace.csv", monkeypatch)
    expected = [[4., 10.], [5., 10.]] if transformed else [[4., 40.], [5., 50.], [6., 60.]]
    np.testing.assert_allclose(data, expected)
    assert len(headers) == 2
    assert "run_3_w" in choices[0][2]


@pytest.mark.parametrize("source_swapped", [False, True])
def test_select_secondary_heatmap_uses_its_grid_and_coordinate_names(
    mixed_plot, tmp_path, monkeypatch, source_swapped,
):
    _window, target, runs = mixed_plot
    # These synthetic layers deliberately use different quantities; accept the
    # normal axis/colour-scale warning before testing their export selection.
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda _box: qtw.QMessageBox.StandardButton.Ok)
    _drop(target, runs["other_heatmap"])
    layer = _added(target, runs["other_heatmap"])
    if source_swapped:
        assert layer.from_win.set_plot_axes_swapped(True)
        _wait_for(lambda: not layer.from_win.worker.running)
    layer.from_win.dataGrid = np.asarray(layer.from_win.dataGrid) + 100
    assert layer.refresh(source_ready=True)
    layer.set_visible(False)
    dialog = _dialog(target)
    label = _select(dialog, layer)
    assert f"Run {layer.from_win.ds.run_id}" in label and "q" in label and "heatmap" in label
    assert "columnMode" not in dialog.controls.fields
    headers, data, _choices = _save(target, dialog, tmp_path / "heatmap.csv", monkeypatch)
    assert headers == ["u", "v", "q"]
    x, y = np.meshgrid(layer.axis_data["x"], layer.axis_data["y"])
    np.testing.assert_allclose(data, np.column_stack((x.ravel(), y.ravel(), layer.data_grid.ravel())))
    assert np.all(data[:, 2] >= 110)


def test_all_traces_option_and_hidden_individual_trace(mixed_plot, tmp_path, monkeypatch):
    _window, target, runs = mixed_plot
    if target.operation_kind == "plot2d":
        _drop(target, runs["line"])
        _added(target, runs["line"])
    _drop(target, runs["other_line"])
    line = _added(target, runs["other_line"])
    line.setData(x=[4., 5., 6.], y=[40., 50., 60.])
    line.hide()
    dialog = _dialog(target)
    measurements = dialog.controls.fields["measurements"]
    measurements.setCurrentIndex(measurements.findData("all_traces"))
    qtw.QApplication.processEvents()
    assert dialog.controls.fields["columnMode"].isVisible()
    assert "Run 1" in dialog.controls.fields["measurements"].toolTip()
    assert "Run 3" in dialog.controls.fields["measurements"].toolTip()
    assert "Run 3" in dialog.csv_scope.text() and "[hidden]" in dialog.csv_scope.text()
    headers, data, _choices = _save(target, dialog, tmp_path / "all.csv", monkeypatch)
    assert len(headers) == 4 and data.shape == (3, 4)
    _select(dialog, line)
    assert "columnMode" not in dialog.controls.fields
    assert "Includes the hidden measurement." in dialog.csv_scope.text()
    headers, data, _choices = _save(target, dialog, tmp_path / "hidden.csv", monkeypatch)
    assert len(headers) == 2
    np.testing.assert_allclose(data, [[4., 40.], [5., 50.], [6., 60.]])


def test_removed_selection_does_not_export_another_measurement(mixed_plot, tmp_path, monkeypatch):
    _window, target, runs = mixed_plot
    _drop(target, runs["other_line"])
    line = _added(target, runs["other_line"])
    dialog = _dialog(target)
    _select(dialog, line)
    key = next(key for key, item in target.lines.items() if item is line)
    target.remove_line("", trace_key=key)
    errors = []
    monkeypatch.setattr(target, "show_error", lambda *args: errors.append(args))
    def unexpected_destination(*_args, **_kwargs):
        pytest.fail("A removed measurement must be rejected before choosing a destination")
    monkeypatch.setattr(qtw.QFileDialog, "getSaveFileName", unexpected_destination)
    dialog.ui.exportBtn.click()
    assert errors and "removed" in errors[-1][-1].lower()
    assert not target.__dict__.get("_export_tasks")


def test_switching_between_trace_and_grid_preserves_trace_options(mixed_plot, monkeypatch):
    _window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda _box: qtw.QMessageBox.StandardButton.Ok)
    _drop(target, runs["other_line"])
    line = _added(target, runs["other_line"])
    _drop(target, runs["other_heatmap"])
    layer = _added(target, runs["other_heatmap"])
    dialog = _dialog(target)
    _select(dialog, line)
    exporter = dialog.currentExporter
    exporter.params["values"] = CSV_TRANSFORMED_VALUES
    exporter.params["columnMode"] = "(x,y,y,y) for all plots"
    exporter.params["separator"] = "tab"
    _select(dialog, layer)
    assert "columnMode" not in exporter.params.names
    _select(dialog, line)
    assert exporter.params["values"] == CSV_TRANSFORMED_VALUES
    assert exporter.params["columnMode"] == "(x,y,y,y) for all plots"
    assert exporter.params["separator"] == "tab"
