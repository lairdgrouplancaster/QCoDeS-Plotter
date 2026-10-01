"""Exercise real qPlot windows, Qt controls and PyQtGraph CSV export."""

import csv
import sys
from contextlib import closing

import numpy as np
import pytest
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from PyQt6.QtTest import QTest
from pyqtgraph.exporters import CSVExporter

from qplot.datahandling.readonly import sqlite_read_only_connection
from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import (
    build_line_database,
    build_synthetic_database,
    configure_temp_qplot,
    csv_rows,
    export_real_plot_csv,
    wait_for,
)


@pytest.fixture
def loaded_plot(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    database_path = tmp_path / "overlays.db"
    _run_id, guid, _table = build_line_database(database_path, 64)
    merged_guid = None
    if getattr(request, "param", False):
        _run_id, merged_guid, _table = build_line_database(database_path, 48)
    protected = {
        suffix: (
            path.read_bytes(), path.stat().st_mtime_ns
        ) if (path := database_path.with_name(database_path.name + suffix)).exists()
        else None
        for suffix in ("", "-wal", "-journal")
    }
    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(lambda: not window._database_load_active)
        window.openPlot(guid=guid, show=True)
        plot = window.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        if merged_guid is not None:
            window.openPlot(guid=merged_guid, show=False)
            source = window.windows[-1]
            wait_for(lambda: hasattr(source, "axis_data") and not source.worker.running)
            source.monitor.stop()
            assert window.add_trace_to_plot(
                plot, source._dataset_key, source.param.name, param=source.param,
            )
            wait_for(lambda: len(plot.lines) == 2)
        yield plot
    finally:
        close_main_window(window)
        for suffix, original in protected.items():
            path = database_path.with_name(database_path.name + suffix)
            current = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
            assert current == original


def _assert_overlays(window, viewbox):
    overlays = [window.marquee_highlight, window.marquee_outline, window.marquee_handles]
    if hasattr(window, "hover_pixel_outline"):
        overlays.append(window.hover_pixel_outline)
    for item in overlays:
        assert window._plot_overlay_viewboxes[id(item)] is viewbox
        assert item.parentItem() is viewbox.childGroup
        assert item not in viewbox.addedItems  # ignoreBounds=True
        assert item not in window.plot.items  # CSVExporter walks this list
        assert item not in window.plot.listDataItems()
        assert item not in window.plot.curves


def _drag_in_view(window, start, end, modifiers):
    viewport = window.widget.viewport()
    start = window.widget.mapFromScene(window.vb.mapViewToScene(start))
    end = window.widget.mapFromScene(window.vb.mapViewToScene(end))
    QTest.mousePress(viewport, QtCore.Qt.MouseButton.LeftButton, modifiers, start)
    # Supply pressed-button state explicitly; offscreen QTest.mouseMove does
    # not retain it on every Qt platform.
    midpoint = QtCore.QPoint((start.x() + end.x()) // 2, (start.y() + end.y()) // 2)
    for position in (midpoint, end):
        event = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseMove,
            QtCore.QPointF(position), QtCore.QPointF(viewport.mapToGlobal(position)),
            QtCore.Qt.MouseButton.NoButton, QtCore.Qt.MouseButton.LeftButton, modifiers,
        )
        qtw.QApplication.sendEvent(viewport, event)
    QTest.mouseRelease(viewport, QtCore.Qt.MouseButton.LeftButton, modifiers, end)
    qtw.QApplication.processEvents()


def _assign_trace_axes(window, key, x_axis, y_axis):
    style = window._trace_styles[key]
    style.x_axis, style.y_axis = x_axis, y_axis
    window._apply_trace_style(key, window.lines[key])
    qtw.QApplication.processEvents()
    return window._trace_axis_viewbox(style)


@pytest.mark.parametrize("loaded_plot", [False, True], indirect=True, ids=["single", "merged"])
@pytest.mark.parametrize("suffix,delimiter", [("csv", ","), ("tsv", "\t")])
def test_plot_csv_keeps_traces_across_all_axes_with_active_overlays(
    loaded_plot, tmp_path, monkeypatch, suffix, delimiter,
):
    window = loaded_plot
    lines = list(window.lines.items())
    expected = [np.column_stack(line.getOriginalDataset()) for _key, line in lines]
    baseline = None
    for trace_index, (trace_key, trace) in enumerate(lines):
        if trace_index:
            # Also export when both merged measurements use secondary axes.
            _assign_trace_axes(window, lines[0][0], "Top", "Right")
        for index, (x_axis, y_axis) in enumerate((
            ("Bottom", "Left"), ("Bottom", "Right"), ("Top", "Left"),
            ("Top", "Right"), ("Bottom", "Left"),
        )):
            owner = _assign_trace_axes(window, trace_key, x_axis, y_axis)
            assert trace.getViewBox() is owner
            window.set_marquee_rect(QtCore.QRectF(20, 25, 30, 40))
            window._show_snap_marker(47, 57, owner)
            assert window.marquee_handles.isVisible()
            assert window.snap_marker.isVisible()
            target = tmp_path / f"trace-{trace_index}-{index}.{suffix}"
            assert export_real_plot_csv(monkeypatch, window, target)
            with target.open(newline="", encoding="utf-8") as exported:
                rows = list(csv.reader(exported, delimiter=delimiter))
            assert len(rows[0]) == 2 * len(lines)
            assert len(rows) == 65
            for pair, values in enumerate(expected):
                np.testing.assert_allclose(
                    np.asarray([row[2 * pair:2 * pair + 2] for row in rows[1:len(values) + 1]], dtype=float),
                    values,
                )
                assert all(row[2 * pair:2 * pair + 2] == ["", ""] for row in rows[len(values) + 1:])
            if baseline is None:
                baseline = rows
            assert rows == baseline
            for _key, line in lines:
                assert window.plot.items.count(line) == 1
                assert window.plot.listDataItems().count(line) == 1
                assert window.plot.curves.count(line) == 1
            for overlay in (window.marquee_handles, window.snap_marker):
                assert overlay not in window.plot.items
                assert overlay not in window.plot.listDataItems()
                assert overlay not in window.plot.curves


@pytest.mark.parametrize("loaded_plot", [True], indirect=True)
@pytest.mark.parametrize("control,option", [
    ("fftCheck", "fftMode"), ("subtractMeanCheck", "subtractMeanMode"),
    ("derivativeCheck", "derivativeMode"), ("phasemapCheck", "phasemapMode"),
])
def test_native_processing_controls_follow_all_trace_axes(
    loaded_plot, tmp_path, monkeypatch, control, option,
):
    window = loaded_plot
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    lines = list(window.lines.items())
    check = getattr(window.plot.ctrl, control)
    expected = [np.column_stack(line.getOriginalDataset()) for _key, line in lines]
    check.setChecked(True)
    processed = [np.column_stack(line.getData()).copy() for _key, line in lines]
    window.plot.setDownsampling(ds=4, auto=False, mode="subsample")
    for index, (x_axis, y_axis) in enumerate((
        ("Bottom", "Right"), ("Top", "Left"), ("Top", "Right"), ("Bottom", "Left"),
    )):
        for key, line in lines:
            _assign_trace_axes(window, key, x_axis, y_axis)
            assert line.opts[option]
            assert line.opts["downsample"] == 4
        check.setChecked(False)
        assert all(not line.opts[option] for _key, line in lines)
        window.plot.setDownsampling(ds=2, auto=False, mode="subsample")
        check.setChecked(True)
        for (_key, line), values in zip(lines, processed, strict=True):
            assert line.opts[option]
            assert line.opts["downsample"] == 2
            np.testing.assert_allclose(np.column_stack(line.getData()), values[::2])
        window.plot.setDownsampling(ds=4, auto=False, mode="subsample")
        # Native CSV semantics export full data supplied to PlotDataItem,
        # before PyQtGraph's mapping, FFT and display reduction.
        target = tmp_path / f"processed-{index}.csv"
        assert export_real_plot_csv(monkeypatch, window, target)
        rows = csv_rows(target)
        assert len(rows[0]) == 4
        for pair, values in enumerate(expected):
            np.testing.assert_allclose(
                np.asarray([row[2 * pair:2 * pair + 2] for row in rows[1:len(values) + 1]], dtype=float),
                values,
            )
    assert errors == []


def test_secondary_axis_csv_exports_qplot_processed_measurements(
    loaded_plot, tmp_path, monkeypatch,
):
    window = loaded_plot
    operation = next(
        window.oper_widget.list_options.item(index)
        for index in range(window.oper_widget.list_options.count())
        if window.oper_widget.list_options.item(index).label == "dy/dx"
    )
    previous_worker = window.worker
    operation.input.setChecked(True)
    window.oper_widget.apply_but.click()
    wait_for(lambda: window.worker is not previous_worker and not window.worker.running)
    np.testing.assert_allclose(window.axis_data["y"], 1)
    for index, axes in enumerate((("Bottom", "Right"), ("Top", "Left"), ("Top", "Right"))):
        _assign_trace_axes(window, window.label, *axes)
        target = tmp_path / f"derivative-{index}.csv"
        assert export_real_plot_csv(monkeypatch, window, target)
        np.testing.assert_allclose(
            np.asarray(csv_rows(target)[1:], dtype=float),
            np.column_stack((window.axis_data["x"], np.ones(64))),
        )


def test_secondary_axis_csv_failure_preserves_existing_export(
    loaded_plot, tmp_path, monkeypatch,
):
    window = loaded_plot
    _assign_trace_axes(window, window.label, "Top", "Right")
    target = tmp_path / "preserved.csv"
    original = b"existing export\n"
    target.write_bytes(original)
    files_before = set(tmp_path.iterdir())
    native_export = CSVExporter.export
    staging_paths = []
    errors = []
    monkeypatch.setattr(window, "show_error", lambda *error: errors.append(error))

    def fail_after_serialization(exporter, fileName=None):
        staging_paths.append(fileName)
        assert fileName != str(target)
        native_export(exporter, fileName=fileName)
        raise RuntimeError("CSV writer failed after serialization")

    monkeypatch.setattr(CSVExporter, "export", fail_after_serialization)
    assert not export_real_plot_csv(monkeypatch, window, target)
    assert errors[0][0] == "Plot Export Failed"
    assert len(staging_paths) == 1
    assert target.read_bytes() == original
    assert set(tmp_path.iterdir()) == files_before


@pytest.mark.parametrize("loaded_plot", [True], indirect=True)
@pytest.mark.parametrize("axes", [("Bottom", "Right"), ("Top", "Left"), ("Top", "Right")])
def test_removing_secondary_axis_trace_unregisters_it_for_export_and_controls(
    loaded_plot, tmp_path, monkeypatch, axes,
):
    window = loaded_plot
    key, line = next((key, line) for key, line in window.lines.items() if line is not window.line)
    owner = _assign_trace_axes(window, key, *axes)
    # The legacy side setter must use the same path and retain the top axis.
    line.set_side("left" if axes[1] == "Right" else "right")
    assert window._trace_styles[key].x_axis == axes[0]
    _assign_trace_axes(window, key, *axes)
    window.remove_line(line.label, key)
    assert line not in owner.addedItems
    assert line.scene() is None
    for registry in (window.plot.items, window.plot.listDataItems(), window.plot.curves):
        assert line not in registry
    window.plot.ctrl.fftCheck.setChecked(True)
    assert not line.opts["fftMode"]
    target = tmp_path / "removed.csv"
    assert export_real_plot_csv(monkeypatch, window, target)
    rows = csv_rows(target)
    assert len(rows[0]) == 2
    np.testing.assert_allclose(
        np.asarray(rows[1:], dtype=float),
        np.column_stack((np.arange(64), np.arange(64) + 10)),
    )


def test_plot_csv_and_native_controls_exclude_selection_overlays(
    loaded_plot, tmp_path, monkeypatch,
):
    window = loaded_plot
    # Qt reports exceptions in signal handlers to excepthook, not the caller.
    exceptions = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: exceptions.append(error))
    expected = np.column_stack((np.arange(64), np.arange(64) + 10))
    baseline = None
    for stage in ("before", "during", "after"):
        if stage == "during":
            _drag_in_view(
                window, QtCore.QPointF(20, 25), QtCore.QPointF(50, 65),
                QtCore.Qt.KeyboardModifier.AltModifier,
            )
            assert window.marquee_handles.isVisible()
            assert len(window.marquee_handles.getData()[0]) == 8
            handle = window._marquee_handle_points()["e"]
            scene_pos = window.vb.mapViewToScene(handle)
            assert window.marquee_drag_mode_at(scene_pos) == "e"
            window.begin_marquee_drag(handle, "e")
            window.drag_marquee_to(QtCore.QPointF(55, handle.y()))
            window.finish_marquee_drag()
            window._show_snap_marker(47, 57, window.vb)
        elif stage == "after":
            QTest.keyClick(window, QtCore.Qt.Key.Key_Escape)
            assert window.marquee is None
            assert not window.marquee_handles.isVisible()
            # Hidden items still retain the eight coordinates.
            assert len(window.marquee_handles.getData()[0]) == 8
            window._hide_snap_marker()

        _assert_overlays(window, window.vb)
        assert window.plot.listDataItems() == [window.line]
        target = tmp_path / f"{stage}.csv"
        assert export_real_plot_csv(monkeypatch, window, target)
        rows = csv_rows(target)
        assert len(rows[0]) == 2
        np.testing.assert_allclose(np.asarray(rows[1:], dtype=float), expected)
        if baseline is None:
            baseline = rows
        assert rows == baseline

        # Use the actual controls and their connected native processing slots.
        window.plot.setDownsampling(ds=8, auto=False, mode="peak")
        assert window.line.opts["downsample"] == 8
        assert len(window.line.getData()[0]) == 16
        window.plot.ctrl.fftCheck.setChecked(True)
        assert window.line.opts["fftMode"]
        window.plot.ctrl.fftCheck.setChecked(False)
        window.plot.setDownsampling(ds=False, auto=False)
        qtw.QApplication.processEvents()
        assert exceptions == []

    # Selection and pointer decorations must never extend automatic bounds.
    window.vb.autoRange(padding=0)
    baseline_bounds = window.vb.viewRange()
    window.marquee = QtCore.QRectF(-1000, -1000, 2000, 2000)
    window.marquee_outline.setRect(window.marquee)
    window.marquee_highlight.setRect(window.marquee)
    window.marquee_outline.show()
    window.marquee_highlight.show()
    window._update_marquee_handles()
    window._show_snap_marker(2000, 2000, window.vb)
    assert window.snap_marker not in window.vb.addedItems
    window.vb.autoRange(padding=0)
    np.testing.assert_allclose(window.vb.viewRange(), baseline_bounds)


@pytest.mark.parametrize("method", ["subsample", "mean", "peak"])
@pytest.mark.parametrize("logarithmic", [False, True])
def test_snap_and_statistics_use_full_samples_after_downsampling(
    loaded_plot, method, logarithmic,
):
    window = loaded_plot
    line = window.line
    line.setLogMode(logarithmic, logarithmic)
    window.plot.getAxis("bottom").setLogMode(logarithmic)
    window.plot.getAxis("left").setLogMode(logarithmic)
    window.plot.setDownsampling(ds=8, auto=False, mode=method)
    window.plot.ctrl.clipToViewCheck.setChecked(True)
    transform = np.log10 if logarithmic else np.asarray
    window.vb.setRange(xRange=transform([40, 54]), yRange=transform([50, 64]), padding=0)
    qtw.QApplication.processEvents()
    assert len(line.getData()[0]) < 64

    point = QtCore.QPointF(float(transform(47)), float(transform(57)))
    scene_pos = window.vb.mapViewToScene(point)
    nearest = window._nearest_trace_point(scene_pos)
    assert window._nearest_1d_array_index(point.x()) == 47
    assert (nearest.x_value, nearest.y_value, nearest.point_number) == (47, 57, 48)
    assert nearest.x_view_value == pytest.approx(point.x())
    assert nearest.y_view_value == pytest.approx(point.y())
    window.snap_to_trace_action.setChecked(True)
    window.mouseMoved(scene_pos)
    assert "47" in window.pos_labels["x"].text()
    assert "57" in window.pos_labels["y"].text()
    assert "point 47" in window.trace_label.text()
    np.testing.assert_allclose(window.snap_marker.getData(), [[point.x()], [point.y()]])

    # Select underlying points that are absent from the reduced display array.
    window.set_marquee_rect(QtCore.QRectF(
        QtCore.QPointF(float(transform(45)), float(transform(54.9))),
        QtCore.QPointF(float(transform(49)), float(transform(59.1))),
    ))
    np.testing.assert_array_equal(window._marquee_line_values(), np.arange(55, 60))
    stats = window._marquee_stats_text()
    assert stats.startswith("5 points")
    assert f"Average: {window.formatNum(57)}" in stats
    assert f"Max: {window.formatNum(59)}" in stats
    assert f"Min: {window.formatNum(55)}" in stats
    assert f"Standard deviation: {window.formatNum(np.std(np.arange(55, 60)))}" in stats
    window.plot.setDownsampling(ds=False, auto=False)
    assert window._marquee_stats_text() == stats


@pytest.mark.parametrize("control", [
    "fftCheck", "subtractMeanCheck", "derivativeCheck", "phasemapCheck",
])
def test_native_processing_readouts_remain_aligned_after_downsampling(
    loaded_plot, control,
):
    window = loaded_plot
    getattr(window.plot.ctrl, control).setChecked(True)
    full_x, full_y = window.line.getData()
    full_x, full_y = full_x.copy(), full_y.copy()
    window.plot.setDownsampling(ds=8, auto=False, mode="peak")
    assert len(window.line.getData()[0]) < len(full_x)
    index = 18
    point = QtCore.QPointF(float(full_x[index]), float(full_y[index]))
    nearest = window._nearest_trace_point(window.vb.mapViewToScene(point))
    assert nearest.point_number == index + 1
    assert nearest.x_value == pytest.approx(full_x[index])
    assert nearest.y_value == pytest.approx(full_y[index])
    # Statistics use the native processed values, before display reduction.
    window.marquee = QtCore.QRectF(
        QtCore.QPointF(float(full_x.min()) - 1, float(full_y.min()) - 1),
        QtCore.QPointF(float(full_x.max()) + 1, float(full_y.max()) + 1),
    )
    np.testing.assert_allclose(window._marquee_line_values(), full_y)


def test_automatic_downsampling_keeps_measurement_indices_and_statistics(loaded_plot):
    window = loaded_plot
    x = np.arange(12000, dtype=float)
    window.line.setData(x=x, y=x + 10)
    window.plot.setDownsampling(auto=True, mode="peak")
    window.vb.setRange(xRange=[0, 12000], yRange=[10, 12010], padding=0)
    qtw.QApplication.processEvents()
    assert len(window.line.getData()[0]) < len(x)
    scene_pos = window.vb.mapViewToScene(QtCore.QPointF(8443, 8453))
    nearest = window._nearest_trace_point(scene_pos)
    assert (nearest.x_value, nearest.y_value, nearest.point_number) == (8443, 8453, 8444)
    window.set_marquee_rect(QtCore.QRectF(8440, 8449.9, 9, 9.2))
    np.testing.assert_array_equal(window._marquee_line_values(), np.arange(8450, 8460))


def test_heatmap_overlays_stay_outside_registry_when_axes_change(
    tmp_path, monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    database_path = tmp_path / "heatmap-overlays.db"
    _line_run, heatmap_run = build_synthetic_database(database_path)
    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(lambda: not window._database_load_active)
        with closing(sqlite_read_only_connection(database_path)) as connection:
            guid = connection.execute(
                "SELECT guid FROM runs WHERE run_id = ?", (heatmap_run,),
            ).fetchone()[0]
        window.openPlot(guid=guid, show=True)
        plot = window.windows[-1]
        wait_for(lambda: hasattr(plot, "dataGrid") and not plot.worker.running)
        plot.monitor.stop()
        plot.set_marquee_rect(QtCore.QRectF(-0.5, -0.2, 1.0, 0.4))
        for x_axis, y_axis in (
            ("Top", "Right"), ("Bottom", "Right"),
            ("Top", "Left"), ("Bottom", "Left"),
        ):
            plot._set_layer_axes(plot, x_axis, y_axis)
            owner = plot._primary_heatmap_viewbox()
            _assert_overlays(plot, owner)
            assert plot.marquee_handles.getViewBox() is owner
            assert plot.marquee_handles.isVisible()
            handle = plot._marquee_handle_points()["ne"]
            assert plot.marquee_drag_mode_at(owner.mapViewToScene(handle)) == "ne"
            assert plot.plot.listDataItems() == []
        plot.clear_marquee()
        _assert_overlays(plot, plot.vb)
    finally:
        close_main_window(window)
