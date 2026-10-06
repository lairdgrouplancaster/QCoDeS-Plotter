"""Screen-space snapping on narrow sweeps with large coordinate offsets."""

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.windows import main as main_window
from qplot.windows._plot1d_snap import Plot1DSnapMixin
from qplot.windows.plot1d import plot1d
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for
from tests.windows.test_plot_overlay_integration import _assign_trace_axes


@pytest.mark.parametrize("offset", [0.0, 5e9, -5e9, 1e12])
@pytest.mark.parametrize("vertical", [False, True])
@pytest.mark.parametrize("size", [(640, 480), (901, 537)])
def test_scene_selection_is_translation_invariant(offset, vertical, size):
    widget = pg.PlotWidget()
    widget.resize(*size)
    widget.show()
    viewbox = widget.getPlotItem().vb
    x = np.arange(64, dtype=float) * 100
    y = np.full(64, 10.0)
    if vertical:
        x, y = y, x
    x += offset
    y += offset
    try:
        # Exercise the initial range, zoom and pan with the same geometry.
        for start, stop in [(-2, 65), (12, 30), (35, 53)]:
            sweep_range = [offset + start * 100, offset + stop * 100]
            flat_range = [offset + 9, offset + 11]
            viewbox.setRange(
                xRange=flat_range if vertical else sweep_range,
                yRange=sweep_range if vertical else flat_range,
                padding=0,
            )
            qtw.QApplication.processEvents()
            for index in range(max(0, start), min(64, stop)):
                point = viewbox.mapViewToScene(QtCore.QPointF(x[index], y[index]))
                # A small scene displacement checks distances as well as index.
                pointer = point + QtCore.QPointF(0.25, -0.125)
                sample, distance = Plot1DSnapMixin._nearest_scene_trace_sample(
                    x, y, pointer, viewbox,
                )
                assert sample.point_number == index + 1
                assert (sample.x_value, sample.y_value) == (x[index], y[index])
                assert distance == pytest.approx(0.25**2 + 0.125**2, abs=0.02)
    finally:
        widget.close()
        widget.deleteLater()


@pytest.mark.parametrize("log_x,log_y", [(True, False), (False, True), (True, True)])
def test_narrow_log_sweeps_keep_physical_values(log_x, log_y):
    widget = pg.PlotWidget()
    widget.resize(813, 521)
    widget.show()
    raw_x = 5e9 + np.arange(64) * 100
    raw_y = 2e9 + np.arange(64) * 50
    x = np.log10(raw_x) if log_x else raw_x
    y = np.log10(raw_y) if log_y else raw_y
    viewbox = widget.getPlotItem().vb
    try:
        for start, stop in [(0, 63), (15, 31), (35, 51)]:
            viewbox.setRange(
                xRange=[x[start], x[stop]], yRange=[y[start], y[stop]], padding=0,
            )
            qtw.QApplication.processEvents()
            for index in range(start, stop + 1):
                point = viewbox.mapViewToScene(QtCore.QPointF(x[index], y[index]))
                sample, distance = Plot1DSnapMixin._nearest_scene_trace_sample(
                    x, y, point, viewbox, raw_x, raw_y,
                )
                assert sample.point_number == index + 1
                assert (sample.x_value, sample.y_value) == (raw_x[index], raw_y[index])
                assert (sample.x_view_value, sample.y_view_value) == (x[index], y[index])
                assert distance < 0.001
    finally:
        widget.close()
        widget.deleteLater()


def test_secondary_trace_competes_in_scene_units_after_zoom_and_pan():
    widget = pg.PlotWidget()
    widget.resize(791, 513)
    widget.show()
    plot = widget.getPlotItem()
    secondary_view = pg.ViewBox()
    plot.scene().addItem(secondary_view)
    x = 5e9 + np.arange(64) * 100
    primary = plot.plot(x=x, y=np.full(64, 10.0))
    secondary_y = 2e9 + np.arange(64) * 100
    secondary = pg.PlotDataItem(x=x, y=secondary_y)
    secondary_view.addItem(secondary)
    window = plot1d.__new__(plot1d)
    window.plot = plot
    window.right_vb = secondary_view
    window.lines = {"primary": primary, "secondary": secondary}
    try:
        for start, stop in [(0, 63), (12, 30), (35, 53)]:
            qtw.QApplication.processEvents()
            secondary_view.setGeometry(plot.vb.sceneBoundingRect())
            plot.vb.setRange(xRange=[x[start], x[stop]], yRange=[9, 11], padding=0)
            secondary_view.setRange(
                xRange=[x[start], x[stop]],
                yRange=[secondary_y[start], secondary_y[stop]], padding=0,
            )
            qtw.QApplication.processEvents()
            for index in range(start + 1, stop):
                pointer = secondary_view.mapViewToScene(QtCore.QPointF(x[index], secondary_y[index]))
                pointer += QtCore.QPointF(0.2, -0.1)
                # Independent, point-by-point Qt oracle; traces have very
                # different physical Y scales but compete in the same pixels.
                candidates = []
                for label, line in window.lines.items():
                    owner = line.getViewBox()
                    for sample_index, (sx, sy) in enumerate(zip(*line.getData(), strict=True)):
                        point = owner.mapViewToScene(QtCore.QPointF(sx, sy))
                        distance = (point.x() - pointer.x())**2 + (point.y() - pointer.y())**2
                        candidates.append((distance, label, sample_index))
                _, expected_label, expected_index = min(candidates)
                nearest = window._nearest_trace_point(pointer)
                assert nearest.label == expected_label
                assert nearest.point_number == expected_index + 1
                assert nearest.viewbox is window.lines[expected_label].getViewBox()
    finally:
        secondary_view.removeItem(secondary)
        plot.scene().removeItem(secondary_view)
        widget.close()
        widget.deleteLater()


def _protected_state(database_path):
    return {
        suffix: (path.read_bytes(), path.stat().st_mtime_ns)
        if (path := database_path.with_name(database_path.name + suffix)).exists()
        else None
        for suffix in ("", "-wal", "-journal")
    }


@pytest.fixture
def ghz_plot(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path)
    database_path = tmp_path / "ghz-snap.db"
    initialise_or_create_database_at(str(database_path), journal_mode="DELETE")
    experiment = load_or_create_experiment("snap_precision", sample_name="GHz")
    frequency = ManualParameter("frequency", label="Frequency", unit="Hz")
    signal = ManualParameter("signal", label="Signal")
    measurement = Measurement(exp=experiment, name="narrow_ghz_line")
    measurement.register_parameter(frequency)
    measurement.register_parameter(signal, setpoints=(frequency,))
    with measurement.run(write_in_background=False) as datasaver:
        for value in 5e9 + np.arange(64) * 100:
            datasaver.add_result((frequency, float(value)), (signal, 10.0))
        dataset = datasaver.dataset
        guid = dataset.guid
    dataset.conn.close()
    experiment.conn.close()
    protected = _protected_state(database_path)
    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(lambda: not window._database_load_active)
        window.monitor.stop()
        prior_plot_count = len(window.windows)
        window.openPlot(guid=guid, show=True)
        wait_for(lambda prior_plot_count=prior_plot_count: len(window.windows) > prior_plot_count)
        plot = window.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        np.testing.assert_array_equal(plot.line.getOriginalDataset()[0], 5e9 + np.arange(64) * 100)
        np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], np.full(64, 10.0))
        yield plot
    finally:
        close_main_window(window)
        assert _protected_state(database_path) == protected


@pytest.mark.parametrize("method", [None, "subsample", "mean", "peak"])
@pytest.mark.parametrize("logarithmic", [False, True])
def test_real_ghz_plot_toolbar_and_marker_follow_pointer(ghz_plot, method, logarithmic):
    window = ghz_plot
    window.resize(1103, 731)
    # Use the actual user-facing action and scene signal, with floating-point
    # positions so tests can put the pointer exactly on plotted coordinates.
    window.snap_to_trace_action.trigger()
    assert window.snap_to_trace_action.isChecked()
    window.plot.setLogMode(x=logarithmic, y=logarithmic)
    if method is not None:
        window.plot.setDownsampling(ds=8, auto=False, mode=method)
        assert len(window.line.getData()[0]) < 64
    raw_x = 5e9 + np.arange(64) * 100
    raw_y = np.full(64, 10.0)
    x = np.log10(raw_x) if logarithmic else raw_x
    y = np.log10(raw_y) if logarithmic else raw_y
    for x_axis, y_axis in [
        ("Bottom", "Left"), ("Bottom", "Right"),
        ("Top", "Left"), ("Top", "Right"),
    ]:
        owner = _assign_trace_axes(window, window.label, x_axis, y_axis)
        for start, stop in [(0, 63), (12, 30), (35, 53)]:
            owner.setRange(
                xRange=[x[start], x[stop]],
                yRange=[y[0] - 0.1, y[0] + 0.1], padding=0,
            )
            qtw.QApplication.processEvents()
            for index in range(start, stop + 1):
                point = owner.mapViewToScene(QtCore.QPointF(x[index], y[index]))
                nearest = window._nearest_trace_point(point)
                assert nearest.point_number == index + 1
                assert nearest.x_value == raw_x[index]
                window.plot.scene().sigMouseMoved.emit(point)
                assert window.pos_labels["index"].text() == f"[{index}]"
                assert f"snapped to point {index})" in window.trace_label.text()
                assert window.pos_labels["x"].text() == f"x = {window.formatNum(raw_x[index])};"
                assert window.pos_labels["y"].text() == f"y = {window.formatNum(raw_y[index])}"
                assert window._snap_marker_view is owner
                np.testing.assert_array_equal(window.snap_marker.getData(), [[x[index]], [y[index]]])

            # Also send a real viewport mouse event at an interior sample.
            # Integer pixel rounding is much smaller than sample spacing.
            index = (start + stop) // 2
            point = owner.mapViewToScene(QtCore.QPointF(x[index], y[index]))
            pixel = window.widget.mapFromScene(point)
            viewport = window.widget.viewport()
            event = QtGui.QMouseEvent(
                QtCore.QEvent.Type.MouseMove,
                QtCore.QPointF(pixel), QtCore.QPointF(viewport.mapToGlobal(pixel)),
                QtCore.Qt.MouseButton.NoButton, QtCore.Qt.MouseButton.NoButton,
                QtCore.Qt.KeyboardModifier.NoModifier,
            )
            qtw.QApplication.sendEvent(viewport, event)
            qtw.QApplication.processEvents()
            assert window.pos_labels["index"].text() == f"[{index}]"
