"""Mix real QCoDeS line and heatmap datasets without altering their source DB."""

import sys
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw
from pyqtgraph.exporters import CSVExporter
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.windows import main as main_window
from qplot.windows._dragdrop import make_run_preview_mime
from tests._window_lifecycle import close_main_window
from tests.windows.test_heatmap_layer_integration import (
    _assert_reader_artifact_invariant,
    _configure_temp_qplot,
    _database_artifact_state,
    _PreviewDropEvent,
    _wait_for,
)


@pytest.fixture(params=["line", "heatmap"])
def mixed_plot(request, tmp_path, monkeypatch):
    _configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "mixed.db"
    initialise_or_create_database_at(str(path))
    experiment = load_or_create_experiment("mixed", sample_name="test")
    runs = {}
    specs = [
        ("line", "y", ("x",), ("V",), "A"),
        ("heatmap", "z", ("y", "x"), ("A", "V"), "A"),
        ("other_line", "w", ("u",), ("K",), "Hz"),
        ("other_heatmap", "q", ("v", "u"), ("T", "K"), "V"),
        ("third_line", "r", ("t",), ("m",), "m"),
    ]
    for key, name, axes, units, unit in specs:
        coordinates = [ManualParameter(axis, unit=axis_unit) for axis, axis_unit in zip(axes, units, strict=True)]
        signal = ManualParameter(name, unit=unit)
        measurement = Measurement(exp=experiment)
        for coordinate in coordinates:
            measurement.register_parameter(coordinate)
        measurement.register_parameter(signal, setpoints=tuple(coordinates))
        with measurement.run() as saver:
            if len(axes) == 1:
                for x in range(3):
                    saver.add_result((coordinates[0], x), (signal, x))
            else:
                for y in (10, 20):
                    for x in range(3):
                        saver.add_result((coordinates[0], y), (coordinates[1], x), (signal, y + x))
            runs[key] = SimpleNamespace(guid=saver.dataset.guid, name=name, axes=axes)
    experiment.conn.close()
    artifacts = _database_artifact_state(path)
    window = main_window.MainWindow()
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.config.config["user_preference"]["confirm_close"] = False
    window.config.config["user_preference"]["confirm_close_all"] = False
    exceptions = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: exceptions.append(args))
    try:
        window.close_database(status=False)
        assert window.load_file(str(path))
        _wait_for(lambda: not window._database_load_active and window._selected_run_guid)
        window.openPlot(guid=runs[request.param].guid)
        _wait_for(lambda: bool(window.windows))
        target = window.windows[-1]
        _wait_for(lambda: not target.worker.running and (
            hasattr(target, "dataGrid") if request.param == "heatmap" else len(target.axis_data["x"]) == 3
        ))
        yield window, target, runs
    finally:
        close_main_window(window)
        _assert_reader_artifact_invariant(path, artifacts)
        assert exceptions == []


def _drop(target, run, *, pending=False):
    event = _PreviewDropEvent(make_run_preview_mime(
        run.guid, run.name, None if pending else run.axes, axes_pending=pending,
    ))
    assert target._handle_preview_drag_drop(event)
    assert event.accepted
    assert not event.ignored


def _added(target, run):
    registry_name = "lines" if len(run.axes) == 1 else "heatmaps"
    def find():
        return next((item for key, item in target.__dict__.get(registry_name, {}).items()
                     if getattr(key, "parameter_name", None) == run.name), None)
    _wait_for(lambda: find() is not None)
    layer = find()
    _wait_for(lambda: layer.getData()[0] is not None if registry_name == "lines" else layer.geometry is not None)
    return layer


def _assert_scene_data_order(target):
    """Check Qt's drawing order, including graphics parents on spare axes."""
    scene_items = target.plot.scene().items()
    positions = []
    for kind, key in target._plot_item_order:
        items = ([target.lines[key]] if kind == "trace" else
                 target._heatmap_render_items(target.heatmaps[key]))
        positions.append([scene_items.index(item) for item in items])
    assert all(max(front) < min(back) for front, back in zip(positions, positions[1:], strict=False))


def _add_ordering_data(target, runs):
    names = ["heatmap" if target.operation_kind == "plot1d" else "line", "other_line", "other_heatmap"]
    for name in names:
        previous = list(target._plot_item_order)
        _drop(target, runs[name])
        renderer = _added(target, runs[name])
        kind = "trace" if len(runs[name].axes) == 1 else "heatmap"
        assert target._plot_item_order == [(kind, renderer.from_win._trace_key), *previous]
        _assert_scene_data_order(target)


def test_new_data_is_on_top_before_dialog_opens_and_after_removal(mixed_plot, monkeypatch):
    _window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    _add_ordering_data(target, runs)
    order = list(target._plot_item_order)
    # Duplicate drops must preserve the user's order.
    _drop(target, runs["other_line"])
    assert target._plot_item_order == order
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    assert [dialog._row_entry(row) for row in range(4)] == order
    dialog.reject()
    key = order[0][1]
    removed_items = target._heatmap_render_items(target.heatmaps[key])
    assert target.remove_heatmap(trace_key=key)
    assert all(item not in target.plot.items and item.scene() is None for item in removed_items)
    assert ("heatmap", key) not in target._plot_item_order
    assert ("heatmap", key) not in target._plot_data_viewboxes
    _drop(target, runs["other_heatmap"])
    _added(target, runs["other_heatmap"])
    assert target._plot_item_order == order
    _assert_scene_data_order(target)


def test_dragging_shared_rows_changes_scene_order_across_axis_pairs(mixed_plot, monkeypatch):
    _window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    _add_ordering_data(target, runs)
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    table = dialog.table
    qtw.QApplication.processEvents()
    # Exercise the table's internal drop handler, moving the oldest data first.
    original = list(target._plot_item_order)
    mime = QtCore.QMimeData()
    mime.setData(table._reorder_mime_type, QtCore.QByteArray())
    accepted = []
    event = SimpleNamespace(
        source=lambda: table,
        mimeData=lambda: mime,
        position=lambda: QtCore.QPointF(table.visualRect(table.model().index(0, 0)).topLeft()),
        acceptProposedAction=lambda: accepted.append(True),
    )
    table._dragged_rows = [3]
    table.dropEvent(event)
    assert accepted == [True]
    assert target._plot_item_order == [original[3], *original[:3]]
    _assert_scene_data_order(target)
    # Interleave data on the same axes around a dataset on independent axes.
    dialog._move_rows_to_position([2], 1)
    _assert_scene_data_order(target)
    edited = list(target._plot_item_order)
    dialog._move_rows_to_position([0, 2], 4)
    assert target._plot_item_order == [edited[1], edited[3], edited[0], edited[2]]
    _assert_scene_data_order(target)
    edited = list(target._plot_item_order)
    for key, line in target.lines.items():
        target._trace_styles[key].line_width = 4
        target._apply_trace_style(key, line)
        np.testing.assert_allclose(line.getViewBox().viewRange(),
                                   target._trace_axis_viewbox(target._trace_styles[key]).viewRange())
    target.refresh_secondary_heatmaps()
    target.refresh_secondary_lines()
    _wait_for(lambda: all(not layer.from_win.worker.running for layer in target._secondary_lines()))
    assert target._plot_item_order == edited
    _assert_scene_data_order(target)
    dialog.reject()
    target.open_plot_appearance_dialog()
    assert [dialog._row_entry(row) for row in range(4)] == edited
    _assert_scene_data_order(target)


def test_rendered_overlap_follows_order_on_matching_and_spare_axes(mixed_plot, monkeypatch):
    _window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    foreign = runs["heatmap" if target.operation_kind == "plot1d" else "line"]
    _drop(target, foreign)
    added = _added(target, foreign)
    line = target.line if target.operation_kind == "plot1d" else added
    heatmap = added if target.operation_kind == "plot1d" else target
    line_key = next(key for key, item in target.lines.items() if item is line)
    style = target._trace_styles[line_key]
    style.line_color = "#ff0000"
    style.line_width = 8
    target._apply_trace_style(line_key, line)
    # Deliberately overlap a red trace and an opaque green heatmap.
    line.setData([0, 1, 2], [15, 15, 15])
    heatmap.image.setLookupTable(np.tile(np.array([0, 255, 0, 255], dtype=np.uint8), (256, 1)))
    heatmap.image.setOpacity(1)
    target.vb.setRange(xRange=[0, 2], yRange=[5, 25], padding=0)

    def assert_pixel(trace_on_top):
        qtw.QApplication.processEvents()
        scene_point = target.vb.mapViewToScene(QtCore.QPointF(1, 15))
        point = target.widget.mapFromScene(scene_point)
        screenshot = target.widget.viewport().grab().toImage()
        color = screenshot.pixelColor(point)
        if trace_on_top:
            assert color.red() > 220 and color.green() < 30, color.name()
        else:
            assert color.green() > 220 and color.red() < 30, color.name()

    assert_pixel(target.operation_kind == "plot2d")
    # Independent coordinate parents must still obey the same drawing order.
    style.x_axis, style.y_axis = "Top", "Right"
    target._apply_trace_style(line_key, line)
    target.top_right_vb.setRange(xRange=[0, 2], yRange=[5, 25], padding=0)
    assert_pixel(target.operation_kind == "plot2d")
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    dialog._move_rows_to_position([1], 0)
    _assert_scene_data_order(target)
    assert_pixel(target.operation_kind == "plot1d")


@pytest.mark.parametrize("pending", [False, True])
def test_cross_dimension_drops_share_matching_axes_and_keep_samples(mixed_plot, monkeypatch, pending):
    window, target, runs = mixed_plot
    foreign = runs["heatmap" if target.operation_kind == "plot1d" else "line"]
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: pytest.fail(box.text()))
    _drop(target, foreign, pending=pending)
    layer = _added(target, foreign)
    if target.operation_kind == "plot1d":
        assert target._heatmap_axis_sides(layer) == ("Bottom", "Left")
        np.testing.assert_array_equal(layer.data_grid, [[10, 11, 12], [20, 21, 22]])
        np.testing.assert_array_equal(target.line.getData(), [[0, 1, 2], [0, 1, 2]])
        assert target.bar.isVisible()
        assert "z" in target.bar.axis.labelText
        assert "A" in target.bar.axis.labelText
        assert layer.image.zValue() > target.line.zValue()
        target.open_heatmap_appearance_dialog()
        assert target._plot_appearance_dialog.table.rowCount() == 2
        target.open_colorbar_scale_dialog()
        assert target.colorbar_scale_dialog.isVisible()
    else:
        style = target._trace_styles[layer.from_win._trace_key]
        assert (style.x_axis, style.y_axis) == ("Bottom", "Left")
        np.testing.assert_array_equal(layer.getData(), [[0, 1, 2], [0, 1, 2]])
        np.testing.assert_array_equal(target.dataGrid, [[10, 11, 12], [20, 21, 22]])
        assert layer.zValue() > target.image.zValue()
        target.open_trace_appearance_dialog()
        assert target._plot_appearance_dialog.table.rowCount() == 2
    # Both renderer types participate in automatic bounds on shared axes.
    bounds_items = [item for _view, items in target._axis_scale_bound_item_groups("y") for item in items]
    assert any(hasattr(item, "setPen") for item in bounds_items)
    assert any(hasattr(item, "setImage") for item in bounds_items)
    assert target._axis_scale_auto_limits("y")[1] >= 20
    count = len(target.lines) + len(target.heatmaps)
    _drop(target, foreign)
    assert len(target.lines) + len(target.heatmaps) == count
    source = layer.from_win
    assert not source.visible
    assert source._merged_trace_users == 1
    source.refreshWindow(force=True)
    _wait_for(lambda: not source.worker.running)
    layer.refresh(source_ready=True)
    assert source._merged_trace_users == 1
    target.set_plot_axes_swapped(True)
    _wait_for(lambda: not target.worker.running)
    layer.refresh(source_ready=True)
    if target.operation_kind == "plot1d":
        np.testing.assert_array_equal(layer.data_grid, [[10, 20], [11, 21], [12, 22]])
        assert target.remove_heatmap(trace_key=source._trace_key)
        assert not target.bar.isVisible()
    else:
        np.testing.assert_array_equal(layer.getData(), [[0, 1, 2], [0, 1, 2]])
        target.remove_line(layer.label, source._trace_key)
    assert source._merged_trace_users == 0
    assert all(source._trace_key != key for key in target.lines)
    assert all(source._trace_key != key for key in target.heatmaps)
    assert window.dataset_holder[target._dataset_key].users >= 1


def test_mixed_drop_uses_spare_axes_and_asks_only_for_real_mismatches(mixed_plot, monkeypatch):
    _window, target, runs = mixed_plot
    if target.operation_kind == "plot1d":
        first = runs["other_heatmap"]
        second = runs["heatmap"]
    else:
        first = runs["other_line"]
        second = runs["third_line"]
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: pytest.fail(box.text()))
    _drop(target, first)
    layer = _added(target, first)
    if len(first.axes) == 2:
        assert target._heatmap_axis_sides(layer) == ("Top", "Right")
        assert "q" in target.bar.axis.labelText
    else:
        style = target._trace_styles[layer.from_win._trace_key]
        assert (style.x_axis, style.y_axis) == ("Top", "Right")
    dialogs = []
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box:
                        dialogs.append(box.text()) or qtw.QMessageBox.StandardButton.Cancel)
    before = (len(target.lines), len(target.heatmaps))
    _drop(target, second)
    _wait_for(lambda: bool(dialogs))
    assert (len(target.lines), len(target.heatmaps)) == before
    if len(second.axes) == 2:
        assert "colorscale units do not match" in dialogs[0]
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    _drop(target, second)
    _added(target, second)


def test_mixed_plot_log_controls_follow_each_axis_renderer(mixed_plot, monkeypatch):
    _window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: pytest.fail(box.text()))
    if target.operation_kind == "plot1d":
        target._axis_scale_log_toggled("x", True)
        assert target.plot.getAxis("bottom").logMode
        _drop(target, runs["heatmap"])
        _added(target, runs["heatmap"])
        assert not target.plot.getAxis("bottom").logMode
        assert target.line.opts["logMode"] == [False, False]
        assert not target._axis_scale_log_is_supported("x")
        np.testing.assert_array_equal(target.line.getData(), [[0, 1, 2], [0, 1, 2]])
    else:
        _drop(target, runs["other_line"])
        line = _added(target, runs["other_line"])
        assert not target._axis_scale_log_is_supported("x")
        assert target._axis_scale_log_is_supported("x2")
        target._axis_scale_log_toggled("x2", True)
        assert target.plot.getAxis("top").logMode
        assert line.opts["logMode"] == [True, False]
        np.testing.assert_allclose(line.getData()[0], [np.nan, 0, np.log10(2)])


def test_one_appearance_dialog_switches_controls_and_keeps_edits(mixed_plot, monkeypatch):
    _window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: pytest.fail(box.text()))
    foreign = runs["heatmap" if target.operation_kind == "plot1d" else "line"]
    _drop(target, foreign)
    layer = _added(target, foreign)
    line = target.line if target.operation_kind == "plot1d" else layer
    line_key = next(key for key, value in target.lines.items() if value is line)
    heatmap = layer if target.operation_kind == "plot1d" else target
    heatmap_key = next(key for key, value in target.heatmaps.items() if value is heatmap)
    target.appearance_action.trigger()
    dialog = target._plot_appearance_dialog
    assert dialog.windowTitle() == "Plot Appearance"
    assert dialog is target._trace_appearance_dialog is target._heatmap_appearance_dialog
    assert dialog.table.rowCount() == 2
    assert not isinstance(dialog.heatmap_controls, qtw.QDialog)
    view = next(action.menu() for action in target.menuBar().actions()
                if action.text().replace("&", "") == "View")
    assert [action.text() for action in view.actions() if "Appearance" in action.text()] == ["Appearance…"]
    assert dialog.select_trace(line_key)
    assert dialog.controls_stack.currentWidget() is dialog.trace_controls_panel
    dialog.line_width.setValue(4.5)
    assert target._trace_styles[line_key].line_width == 4.5
    assert dialog.select_heatmap(heatmap_key)
    assert dialog.controls_stack.currentWidget() is dialog.heatmap_controls
    controls = dialog.heatmap_controls
    controls.opacity_slider.setValue(37)
    controls.visible.setChecked(False)
    controls.x_axis.setCurrentText("Top")
    controls.y_axis.setCurrentText("Right")
    assert target._heatmap_opacity(heatmap) == 0.37
    assert not target._heatmap_is_visible(heatmap)
    assert target._heatmap_axis_sides(heatmap) == ("Top", "Right")
    assert target._trace_styles[line_key].line_width == 4.5
    target.open_trace_appearance_dialog(line_key)
    assert target._plot_appearance_dialog is dialog
    assert dialog.line_width.value() == 4.5
    target.open_heatmap_appearance_dialog(heatmap_key)
    assert target._plot_appearance_dialog is dialog
    assert controls.opacity.value() == 37
    assert not controls.visible.isChecked()
    assert controls.x_axis.currentText() == "Top"
    assert controls.y_axis.currentText() == "Right"
    dialog.swap_axes.setChecked(True)
    assert controls.swap_axes.isChecked()
    assert target.plot_axes_swapped()
    _wait_for(lambda: not target.worker.running)
    controls.swap_axes.setChecked(False)
    assert not dialog.swap_axes.isChecked()
    assert not target.plot_axes_swapped()
    _wait_for(lambda: not target.worker.running)
    target.refreshWindow(force=True)
    _wait_for(lambda: not target.worker.running)
    dialog.refresh_rows()
    assert dialog.controls_stack.currentWidget() is controls
    assert controls.opacity.value() == 37
    dialog.reject()
    assert not dialog.isVisible()


def _axis_warning_item(dialog, kind, key):
    row = next(row for row in range(dialog.table.rowCount())
               if dialog._row_entry(row) == (kind, key))
    return dialog.table.item(row, dialog._COL_WARNING)


def _assert_axis_mirrors(target, unused, opposite, bounds):
    axis = target.plot.getAxis(unused)
    other = target.plot.getAxis(opposite)
    assert axis.linkedView() is other.linkedView()
    assert not axis.style["showValues"]
    assert axis.labelText == "" and axis.labelUnits == ""
    number = 0 if unused in ("bottom", "top") else 1
    kwargs = {"xRange" if number == 0 else "yRange": bounds}
    other.linkedView().setRange(**kwargs, padding=0)
    np.testing.assert_allclose(axis.range, other.range)
    np.testing.assert_allclose(axis.range, bounds)


@pytest.mark.parametrize("kind", ["trace", "heatmap"])
def test_removing_dataset_releases_spare_axes_and_readding_assigns_them(mixed_plot, monkeypatch, kind):
    _window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    run = runs["other_line" if kind == "trace" else "other_heatmap"]
    _drop(target, run)
    renderer = _added(target, run)
    key = renderer.from_win._trace_key
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    assert target.plot.getAxis("top").linkedView() is target.top_vb
    assert target.plot.getAxis("right").linkedView() is target.right_vb
    target.top_vb.setXRange(100, 200, padding=0)
    target.right_vb.setYRange(300, 400, padding=0)
    row = next(row for row in range(dialog.table.rowCount()) if dialog._row_entry(row) == (kind, key))
    dialog.table.clearSelection()
    dialog.table.selectRow(row)
    dialog.remove_item_button.click()
    assert dialog.table.rowCount() == 1
    _assert_axis_mirrors(target, "top", "bottom", (2, 8))
    _assert_axis_mirrors(target, "right", "left", (3, 9))
    _drop(target, run)
    _added(target, run)
    assert target.plot.getAxis("top").linkedView() is target.top_vb
    assert target.plot.getAxis("right").linkedView() is target.right_vb
    assert target.plot.getAxis("top").style["showValues"]
    assert target.plot.getAxis("right").style["showValues"]
    target.top_vb.setXRange(100, 200, padding=0)
    target.right_vb.setYRange(300, 400, padding=0)
    np.testing.assert_allclose(target.plot.getAxis("top").range, (100, 200))
    np.testing.assert_allclose(target.plot.getAxis("right").range, (300, 400))
    assert target.plot.getAxis("bottom").linkedView() is target.vb
    assert target.plot.getAxis("left").linkedView() is target.vb


def test_removing_dataset_keeps_shared_axes_assigned(mixed_plot, monkeypatch):
    _window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    for name in ("other_line", "other_heatmap"):
        _drop(target, runs[name])
        _added(target, runs[name])
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    heatmap_key = next(key for key in target.heatmaps if getattr(key, "parameter_name", None) == "q")
    assert dialog.select_heatmap(heatmap_key)
    dialog.remove_item_button.click()
    assert target.plot.getAxis("top").linkedView() is target.top_vb
    assert target.plot.getAxis("right").linkedView() is target.right_vb
    assert target.plot.getAxis("top").style["showValues"]
    assert target.plot.getAxis("right").style["showValues"]


def test_source_removal_mirrors_unused_bottom_and_left_axes(mixed_plot, monkeypatch):
    window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    _drop(target, runs["other_line"])
    _added(target, runs["other_line"])
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    row = next(row for row in range(dialog.table.rowCount()) if dialog._is_primary(*dialog._row_entry(row)))
    dialog.table.clearSelection()
    dialog.table.selectRow(row)
    dialog.remove_item_button.click()
    replacement = next(plot for plot in window.windows if plot is not target and plot.isVisible())
    _wait_for(lambda: not replacement.worker.running)
    _assert_axis_mirrors(replacement, "bottom", "top", (100, 200))
    _assert_axis_mirrors(replacement, "left", "right", (300, 400))
    assert replacement.plot.getAxis("top").style["showValues"]
    assert replacement.plot.getAxis("right").style["showValues"]


def test_source_removal_restores_bottom_tick_numbers_for_remaining_traces(mixed_plot, monkeypatch):
    window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    for name in ("other_line", "third_line"):
        _drop(target, runs[name])
        _added(target, runs[name])
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    row = next(row for row in range(dialog.table.rowCount()) if dialog._is_primary(*dialog._row_entry(row)))
    dialog.table.clearSelection()
    dialog.table.selectRow(row)
    dialog.remove_item_button.click()
    replacement = next(plot for plot in window.windows if plot is not target and plot.isVisible())
    _wait_for(lambda: not replacement.worker.running)
    assert replacement.operation_kind == "plot1d"
    assert len(replacement.lines) == 2
    for side in ("bottom", "left", "top", "right"):
        axis = replacement.plot.getAxis(side)
        assert axis.labelText != ""
        assert axis.style["showValues"], f"Assigned {side} axis must show tick numbers"
    # Clearing and then reassigning the lower axis must also restore numbers.
    dialog = replacement._plot_appearance_dialog
    bottom_key = next(key for key, style in replacement._trace_styles.items() if style.x_axis == "Bottom")
    assert dialog.select_trace(bottom_key)
    dialog.x_axis.setCurrentText("Top")
    assert not replacement.plot.getAxis("bottom").style["showValues"]
    dialog.x_axis.setCurrentText("Bottom")
    assert replacement.plot.getAxis("bottom").style["showValues"]


def test_appearance_axis_warnings_follow_assigned_axes(mixed_plot, monkeypatch):
    _window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: pytest.fail(box.text()))
    _drop(target, runs["other_line"])
    line = _added(target, runs["other_line"])
    key = line.from_win._trace_key
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    item = _axis_warning_item(dialog, "trace", key)
    assert item.icon().isNull()
    assert item.toolTip() == ""
    assert dialog.select_trace(key)
    dialog.x_axis.setCurrentText("Bottom")
    dialog.y_axis.setCurrentText("Left")
    item = _axis_warning_item(dialog, "trace", key)
    assert not item.icon().isNull()
    assert "Bottom axis:" in item.toolTip()
    assert "Left axis:" in item.toolTip()
    assert "u (K)" in item.toolTip() and "x (V)" in item.toolTip()
    assert "w (Hz)" in item.toolTip() and "y (A)" in item.toolTip()
    dialog.x_axis.setCurrentText("Top")
    dialog.y_axis.setCurrentText("Right")
    assert _axis_warning_item(dialog, "trace", key).icon().isNull()


def test_appearance_warns_for_forced_axes_and_updates_after_reorder(mixed_plot, monkeypatch):
    _window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    _drop(target, runs["other_line"])
    _added(target, runs["other_line"])
    _drop(target, runs["third_line"])
    line = _added(target, runs["third_line"])
    key = line.from_win._trace_key
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    item = _axis_warning_item(dialog, "trace", key)
    assert not item.icon().isNull()
    assert "t (m)" in item.toolTip() and "x (V)" in item.toolTip()
    # Trace-primary labels follow the first trace assigned to each side.
    if target.operation_kind == "plot1d":
        dialog._move_rows_to_position([0], 0)
        assert _axis_warning_item(dialog, "trace", key).icon().isNull()
        assert not _axis_warning_item(dialog, "trace", target.label).icon().isNull()
        assert target.plot.getAxis("bottom").labelUnits == "m"
        dialog._move_rows_to_position([0], dialog.table.rowCount())
        assert not _axis_warning_item(dialog, "trace", key).icon().isNull()
        assert _axis_warning_item(dialog, "trace", target.label).icon().isNull()
        assert target.plot.getAxis("bottom").labelUnits == "V"


def test_appearance_warns_for_colorscale_even_on_spare_axes(mixed_plot, monkeypatch):
    _window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    if target.operation_kind == "plot1d":
        _drop(target, runs["heatmap"])
        _added(target, runs["heatmap"])
    _drop(target, runs["other_heatmap"])
    layer = _added(target, runs["other_heatmap"])
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    item = _axis_warning_item(dialog, "heatmap", layer.from_win._trace_key)
    assert not item.icon().isNull()
    assert "Colorscale:" in item.toolTip()
    assert "q (V)" in item.toolTip() and "z (A)" in item.toolTip()
    assert "Bottom axis:" not in item.toolTip()
    assert "Left axis:" not in item.toolTip()
    for row in range(dialog.table.rowCount()):
        kind, key = dialog._row_entry(row)
        if key != layer.from_win._trace_key:
            assert _axis_warning_item(dialog, kind, key).icon().isNull()


def test_shared_appearance_list_adds_removes_and_reorders_both_types(mixed_plot, monkeypatch):
    _window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    sources = []
    for name, kind in (("other_line", "trace"), ("other_heatmap", "heatmap")):
        run = runs[name]
        index = next(index for index in range(dialog.add_item_combo.count())
                     if (entry := dialog.add_item_combo.itemData(index)) is not None
                     and entry[0] == kind and entry[1].parameter_name == run.name)
        dialog.add_item_combo.setCurrentIndex(index)
        assert dialog.add_item_button.isEnabled()
        dialog.add_item_button.click()
        renderer = _added(target, run)
        sources.append((kind, renderer.from_win._trace_key, renderer.from_win))
        assert renderer.from_win._merged_trace_users == 1
        assert dialog._selected_entries() == [(kind, renderer.from_win._trace_key)]
    assert dialog.table.rowCount() == 3
    assert all(dialog.table.item(row, dialog._COL_PREVIEW).icon().isNull() is False
               for row in range(3))
    # Move a heatmap above the traces, then verify actual renderer stacking
    # survives style edits and a refresh of its source.
    heatmap_key = sources[1][1]
    row = next(row for row in range(3) if dialog._row_entry(row) == ("heatmap", heatmap_key))
    dialog._move_rows_to_position([row], 0)
    image = target.heatmaps[heatmap_key].image
    assert image.zValue() > max(line.zValue() for line in target.lines.values())
    assert dialog.select_trace(sources[0][1])
    dialog.line_width.setValue(3)
    sources[1][2].refreshWindow(force=True)
    _wait_for(lambda: not sources[1][2].worker.running)
    assert image.zValue() > max(line.zValue() for line in target.lines.values())
    # The original dataset can be removed whenever other datasets remain.
    primary_row = next(row for row in range(3) if dialog._is_primary(*dialog._row_entry(row)))
    dialog.table.clearSelection()
    dialog.table.selectRow(primary_row)
    assert dialog.remove_item_button.isEnabled()
    dialog.table.clearSelection()
    for kind, key, _source in sources:
        row = next(row for row in range(3) if dialog._row_entry(row) == (kind, key))
        dialog.table.selectionModel().select(
            dialog.table.model().index(row, 0),
            QtCore.QItemSelectionModel.SelectionFlag.Select | QtCore.QItemSelectionModel.SelectionFlag.Rows,
        )
    assert dialog.remove_item_button.isEnabled()
    dialog.refresh_rows()
    assert set(dialog._selected_entries()) == {(kind, key) for kind, key, _source in sources}
    dialog.remove_item_button.click()
    assert dialog.table.rowCount() == 1
    assert not dialog.remove_item_button.isEnabled()
    assert all(source._merged_trace_users == 0 for _kind, _key, source in sources)
    assert dialog.table.item(0, dialog._COL_ID).data(QtCore.Qt.ItemDataRole.UserRole) not in {
        key for _kind, key, _source in sources
    }


def _data_identities(target):
    return [(kind, target._stored_trace_key(key, target.lines[key]) if kind == "trace" else key)
            for kind, key in target._plot_item_order]


@pytest.mark.parametrize("bottom_kind", ["trace", "heatmap"])
@pytest.mark.parametrize("remove_other", [False, True])
def test_removing_source_promotes_bottom_remaining_dataset(
        mixed_plot, monkeypatch, tmp_path, bottom_kind, remove_other):
    window, target, runs = mixed_plot
    errors = []
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(qtw.QMessageBox, "critical", lambda *args: errors.append(args))
    original_entry = target._plot_item_order[0]
    original_key = target._dataset_key
    original_items = ([target.line] if original_entry[0] == "trace" else
                      target._heatmap_render_items(target))
    sources = {}
    for name in ("other_line", "other_heatmap"):
        _drop(target, runs[name])
        sources[name] = _added(target, runs[name]).from_win
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    bottom_name = "other_line" if bottom_kind == "trace" else "other_heatmap"
    bottom_key = sources[bottom_name]._trace_key
    source = sources[bottom_name]
    option = next(source.oper_widget.list_options.item(index)
                  for index in range(source.oper_widget.list_options.count())
                  if source.oper_widget.list_options.item(index).label == "Limit Maximum")
    option.input.setChecked(True)
    operation_limit = "1" if bottom_kind == "trace" else "21"
    option.operation_row.input.setText(operation_limit)
    source.refreshWindow(force=True)
    _wait_for(lambda: not source.worker.running)
    expected_data = np.array(source.axis_data["y"] if bottom_kind == "trace" else source.dataGrid, copy=True)
    row = next(row for row in range(3) if dialog._row_entry(row) == (bottom_kind, bottom_key))
    dialog._move_rows_to_position([row], 3)
    # Preserve style, coordinate assignment, viewport and processing controls
    # across promotions both within and between dimensions.
    trace_key = sources["other_line"]._trace_key
    target._trace_styles[trace_key].line_width = 5.5
    target._apply_trace_style(trace_key, target.lines[trace_key])
    heatmap_key = sources["other_heatmap"]._trace_key
    target._set_layer_opacity(target.heatmaps[heatmap_key], 0.43)
    target._set_layer_axes(target.heatmaps[heatmap_key], "Top", "Right")
    target._colorbar_colormap_name = "magma"
    target._colorbar_manual_levels = (9., 30.)
    target._sync_heatmap_colorbar_items(rescale=True)
    expected_sides = target._heatmap_axis_sides(target.heatmaps[heatmap_key])
    expected_trace_sides = (target._trace_styles[trace_key].x_axis, target._trace_styles[trace_key].y_axis)
    target._axis_scale_viewbox("x").setXRange(-5, 5, padding=0)
    target._axis_scale_viewbox("x2").setXRange(-5, 5, padding=0)
    target._axis_scale_viewbox("y").setYRange(-10, 25, padding=0)
    target._axis_scale_viewbox("y2").setYRange(-10, 25, padding=0)
    before_geometry = target.geometry()
    expected_order = [entry for entry in _data_identities(target)
                      if entry != (original_entry[0], target._trace_key)]
    selected = [original_entry]
    if remove_other:
        other = "heatmap" if bottom_kind == "trace" else "trace"
        other_key = heatmap_key if other == "heatmap" else trace_key
        selected.append((other, other_key))
        expected_order.remove((other, other_key))
    dialog.table.clearSelection()
    for row in range(3):
        if dialog._row_entry(row) in selected:
            dialog.table.selectionModel().select(
                dialog.table.model().index(row, 0),
                QtCore.QItemSelectionModel.SelectionFlag.Select | QtCore.QItemSelectionModel.SelectionFlag.Rows,
            )
    assert dialog.remove_item_button.isEnabled()
    dialog.remove_item_button.click()
    assert errors == []
    replacement = next(win for win in window.windows if win.visible)
    _wait_for(lambda: not replacement.worker.running and not replacement.__dict__.get("_refresh_pending", False))
    assert replacement is not target
    assert replacement._dataset_key == sources[bottom_name]._dataset_key
    assert replacement._trace_key == bottom_key
    assert replacement.param.name == runs[bottom_name].name
    assert replacement.operation_kind == ("plot1d" if bottom_kind == "trace" else "plot2d")
    actual_data = replacement.axis_data["y"] if bottom_kind == "trace" else replacement.dataGrid
    np.testing.assert_array_equal(actual_data, expected_data)
    active_operation = next(replacement.oper_widget.list_order.item(index)
                            for index in range(replacement.oper_widget.list_order.count())
                            if not replacement.oper_widget.list_order.item(index).isHidden())
    assert active_operation.label == "Limit Maximum"
    assert active_operation.input.text() == operation_limit
    assert replacement.geometry() == before_geometry
    for axis in ("x", "y", "x2", "y2"):
        if replacement._axis_scale_axis_is_used(axis):
            dimension = 0 if axis in ("x", "x2") else 1
            assert replacement._axis_scale_viewbox(axis).viewRange()[dimension] == (
                [-5., 5.] if dimension == 0 else [-10., 25.]
            )
    assert _data_identities(replacement) == expected_order
    assert window.dataset_holder[original_key].users == 0
    assert not target.monitor.isActive()
    assert target not in window.windows
    assert all(item not in replacement.plot.items for item in original_items)
    _assert_scene_data_order(replacement)
    new_dialog = replacement._plot_appearance_dialog
    assert new_dialog.isVisible()
    assert new_dialog.table.rowCount() == len(expected_order)
    if not remove_other or bottom_kind == "trace":
        key = replacement.label if bottom_kind == "trace" else trace_key
        style = replacement._trace_styles[key]
        assert style.line_width == 5.5
        assert (style.x_axis, style.y_axis) == expected_trace_sides
    if not remove_other or bottom_kind == "heatmap":
        layer = replacement if bottom_kind == "heatmap" else replacement.heatmaps[heatmap_key]
        assert replacement._heatmap_axis_sides(layer) == expected_sides
        assert replacement._heatmap_opacity(layer) == 0.43
        assert replacement._current_colorbar_colormap_name() == "magma"
        assert replacement._colorbar_manual_levels == (9., 30.)
    if bottom_kind == "heatmap":
        csv_path = tmp_path / "promoted.csv"
        assert replacement._write_heatmap_csv_stage(str(csv_path), CSVExporter(replacement.plot))
        assert csv_path.read_text().splitlines()[0] == "u,v,q"
        assert replacement.heatmap_sample_at(0, 10) is not None
    else:
        assert replacement._cursor_1d_x_data() is not None
    replacement.refreshWindow(force=True)
    _wait_for(lambda: not replacement.worker.running)
    replacement.set_plot_axes_swapped(True)
    _wait_for(lambda: not replacement.worker.running)
    assert _data_identities(replacement) == expected_order
    new_dialog.table.selectAll()
    assert not new_dialog.remove_item_button.isEnabled()
    new_dialog._remove_items()
    assert _data_identities(replacement) == expected_order
    if not remove_other:
        # Repeating removal promotes the final dataset and releases the
        # newly replaced source too, including another dimension change.
        current_source_key = replacement._dataset_key
        new_dialog.table.selectRow(next(row for row in range(2)
                                        if new_dialog._is_primary(*new_dialog._row_entry(row))))
        new_dialog.remove_item_button.click()
        assert errors == []
        final = next(win for win in window.windows if win.visible)
        _wait_for(lambda: not final.worker.running)
        assert final._dataset_key != current_source_key
        assert window.dataset_holder[current_source_key].users == 0
        assert final._plot_appearance_dialog.table.rowCount() == 1
        assert not final._plot_appearance_dialog.remove_item_button.isEnabled()


def test_failed_promotion_keeps_original_plot_and_dataset_ownership(mixed_plot, monkeypatch):
    window, target, runs = mixed_plot
    errors = []
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(qtw.QMessageBox, "critical", lambda *args: errors.append(args))
    original_entry = target._plot_item_order[0]
    _drop(target, runs["other_line"])
    _added(target, runs["other_line"])
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    original_windows = list(window.windows)
    original_order = _data_identities(target)
    users = {key: handle.users for key, handle in window.dataset_holder.items()}
    open_window = window.openWin
    def fail_after_construction(*args, **kwargs):
        open_window(*args, **kwargs)
        raise RuntimeError("Source construction failed")
    monkeypatch.setattr(window, "openWin", fail_after_construction)
    dialog.table.selectRow(next(row for row in range(2) if dialog._row_entry(row) == original_entry))
    dialog.remove_item_button.click()
    assert len(errors) == 1
    assert window.windows == original_windows
    assert _data_identities(target) == original_order
    assert {key: handle.users for key, handle in window.dataset_holder.items()} == users
    assert not target._closed
    assert dialog.isVisible()


def test_source_promotion_cancels_pending_old_refresh(mixed_plot, monkeypatch):
    window, target, runs = mixed_plot
    errors = []
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(qtw.QMessageBox, "critical", lambda *args: errors.append(args))
    original_entry = target._plot_item_order[0]
    original_key = target._dataset_key
    _drop(target, runs["other_line"])
    source = _added(target, runs["other_line"]).from_win
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    dialog.table.selectRow(next(row for row in range(2) if dialog._row_entry(row) == original_entry))
    source.refreshWindow(force=True)
    target.refreshWindow(force=True)
    old_worker = target.worker
    assert old_worker.running
    dialog.remove_item_button.click()
    assert errors == []
    replacement = next(win for win in window.windows if win.visible)
    _wait_for(lambda: not replacement.worker.running and not replacement.__dict__.get("_refresh_pending", False))
    assert old_worker.is_cancelled()
    assert window.dataset_holder[original_key].users == 0
    assert replacement._trace_key == source._trace_key
    assert len(replacement.lines) == 1


@pytest.mark.parametrize("add_method", ["dialog", "drop"])
def test_removed_source_can_be_added_again(mixed_plot, monkeypatch, add_method):
    window, target, runs = mixed_plot
    errors = []
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(qtw.QMessageBox, "critical", lambda *args: errors.append(args))
    original_entry = target._plot_item_order[0]
    original_run = runs["line" if original_entry[0] == "trace" else "heatmap"]
    _drop(target, runs["other_line"])
    _added(target, runs["other_line"])
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    dialog.table.selectRow(next(row for row in range(2) if dialog._row_entry(row) == original_entry))
    dialog.remove_item_button.click()
    assert errors == []
    replacement = next(win for win in window.windows if win.visible)
    _wait_for(lambda: not replacement.worker.running)
    assert replacement._plot_appearance_dialog.table.rowCount() == 1
    assert not replacement._plot_appearance_dialog.remove_item_button.isEnabled()
    if add_method == "drop":
        _drop(replacement, original_run)
    else:
        new_dialog = replacement._plot_appearance_dialog
        index = next(index for index in range(new_dialog.add_item_combo.count())
                     if (entry := new_dialog.add_item_combo.itemData(index)) is not None
                     and entry[1].parameter_name == original_run.name)
        new_dialog.add_item_combo.setCurrentIndex(index)
        new_dialog.add_item_button.click()
        assert errors == []
    renderer = _added(replacement, original_run)
    assert renderer.from_win is not target
    assert replacement._plot_appearance_dialog.table.rowCount() == 2
    _assert_scene_data_order(replacement)

