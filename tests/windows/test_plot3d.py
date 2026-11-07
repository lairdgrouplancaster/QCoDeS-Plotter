"""Real QCoDeS 3D launch, interaction, cuts, export, and protected source reads."""

import itertools
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6 import QtCore, QtGui, QtTest
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.volume import VolumeStore
from qplot.windows._plotWin import plotWidget
from qplot.windows._widgets.operations import OperationEntry, OperationsState
from qplot.windows.main import MainWindow
from qplot.windows.plot3d import plot3d
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import (
    assert_database_artifacts_unchanged_except_trusted_shm,
    configure_temp_qplot,
    trusted_database_artifact_state,
    wait_for,
)


def build_volume(path, array=False, *, gate_points=None):
    initialise_or_create_database_at(str(path), journal_mode="WAL")
    experiment = load_or_create_experiment("3D", "synthetic")
    measurement = Measurement(exp=experiment)
    for name in ("field", "bias", "gate"):
        measurement.register_custom_parameter(name, unit="V", paramtype="array" if array else "numeric")
    measurement.register_custom_parameter("signal", unit="A", setpoints=("field", "bias", "gate"),
                                          paramtype="array" if array else "numeric")
    try:
        with measurement.run(write_in_background=False) as saver:
            gates = [-2., 0., 5.] if gate_points is None else gate_points
            points = np.asarray(list(itertools.product([4., 1., 2.5], [0., 3.], gates)))
            signal = 100 * points[:, 0] + 10 * points[:, 1] + points[:, 2]**2
            if array:
                saver.add_result(*zip(("field", "bias", "gate", "signal"), (*points.T, signal), strict=True))
            else:
                for point, value in zip(points, signal, strict=True):
                    saver.add_result(*zip(("field", "bias", "gate", "signal"), (*point, value), strict=True))
        guid = saver.dataset.guid
        saver.dataset.conn.close()
        return guid
    finally:
        experiment.conn.close()


@pytest.fixture
def main(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path)
    errors = []
    monkeypatch.setattr(plotWidget, "show_error", lambda self, *args: errors.append(args))
    window = MainWindow()
    monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
    window.startupDatabaseTimer.stop()
    window.config.config["user_preference"]["confirm_close"] = False
    window.config.config["user_preference"]["confirm_close_all"] = False
    window.close_database(status=False)
    window.autoPlotBox.setChecked(False)
    yield window, errors
    close_main_window(window)


def loaded(plot):
    return hasattr(plot, "worker") and not plot.worker.running and plot.worker.result is not None


def test_color_dialog_scope_sync_and_symmetric_lock(tmp_path, main):
    window, errors = main
    path = tmp_path / "colour.db"
    guid = build_volume(path)
    window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows))
    plot = window.windows[0]
    wait_for(lambda: loaded(plot) and plot._range_complete)
    plot.open_colorbar_scale_dialog()
    assert not plot.slice_panel.isAncestorOf(plot.symmetric_color)
    plot.colorbar_scope.setCurrentIndex(1)
    assert plot.color_mode.currentData() == plot._color_mode == "slice"
    plot.symmetric_color.setChecked(True)
    assert plot.bar.levels()[0] == -plot.bar.levels()[1]
    plot.color_mode.setCurrentIndex(2)
    assert plot.colorbar_scope.currentData() == "locked"
    levels = tuple(plot.bar.levels())
    plot.symmetric_color.setChecked(False)
    plot.symmetric_color.setChecked(True)
    assert plot._color_mode == "locked"
    assert tuple(plot.bar.levels()) == levels
    plot.select_slice(1)
    wait_for(lambda: loaded(plot) and plot.slice_frame.index == 1)
    assert tuple(plot.bar.levels()) == levels
    plot.colorbar_max_text.setText("10")
    plot.colorbar_max_text.editingFinished.emit()
    assert plot.bar.levels() == (-10., 10.)
    plot.colorbar_auto_radio.click()
    assert plot.colorbar_scope.currentData() == plot.color_mode.currentData() == "slice"
    plot.color_mode.setCurrentIndex(0)
    wait_for(lambda: plot._range_complete)
    assert plot.colorbar_scope.currentData() == "volume"
    assert plot.bar.levels()[0] == -plot.bar.levels()[1]
    assert not errors


def test_compact_depth_display_retains_distinct_planes():
    depths = np.array([-.023999999999999994, 1., np.nextafter(1., 2.), 1.00000000001])
    zeros = np.zeros(depths.size)
    volume = VolumeStore.from_chunks(("depth", "y", "x"), [(depths, zeros, zeros, zeros)])
    try:
        host = SimpleNamespace(volume=volume)
        texts = [plot3d._depth_text(host, value, "depth") for value in depths]
        assert texts[0] == "-0.024"
        assert len(set(texts)) == len(depths)
        for index, text in enumerate(texts):
            assert volume.nearest_index("depth", float(text)) == index
        np.testing.assert_array_equal(volume.axes["depth"], depths)
    finally:
        volume.directory.cleanup()


def test_cursor_includes_depth_in_readout_tooltip_and_copy(tmp_path, main):
    window, errors = main
    path = tmp_path / "cursor.db"
    guid = build_volume(path)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows))
    plot = window.windows[0]
    wait_for(lambda: loaded(plot) and plot._range_complete)
    assert float(plot.pos_labels["depth"].text()) == plot.slice_frame.depth
    plot.mouseMoved(plot.vb.mapViewToScene(QtCore.QPointF(0., 3.)))
    roles = ("x", "y", "depth", "z")
    assert [float(plot.pos_labels[role].text()) for role in roles] == [0., 3., 1., 130.]
    assert plot.pos_labels["index"].text() == "[1,1,0]"
    tooltip = plot.cursor_readout.toolTip()
    assert "Values: (X, Y, depth, value)" in tooltip
    assert "X: gate (V)" in tooltip and "Depth: field (V)" in tooltip
    assert "Value: signal (A)" in tooltip
    assert "Current: [1,1,0]" in tooltip
    qtw.QApplication.processEvents()
    fields = [plot.pos_labels[role] for role in ("index", *roles)]
    assert all(left.geometry().right() < right.geometry().left()
               for left, right in zip(fields, fields[1:], strict=False))
    plot.select_slice(2)
    wait_for(lambda: loaded(plot) and plot.slice_frame.depth == 4.)
    # Refresh the stationary cursor as well as the heatmap during navigation.
    assert [float(plot.pos_labels[role].text()) for role in roles] == [0., 3., 4., 430.]
    assert plot.pos_labels["index"].text() == "[1,1,2]"
    assert plot.copy_coordinates()
    copied = qtw.QApplication.clipboard().text()
    assert copied.startswith("[1,1,2]")
    assert [float(value) for value in copied.split("(")[1].rstrip(")").split(",")] == [0., 3., 4., 430.]
    plot.vb.setRange(xRange=(-3, 10), yRange=(-1, 4), padding=0)
    qtw.QApplication.processEvents()
    plot.mouseMoved(plot.vb.mapViewToScene(QtCore.QPointF(9., 3.)))
    assert [float(plot.pos_labels[role].text()) for role in roles[:3]] == [9., 3., 4.]
    assert plot.pos_labels["z"].text() == "—"
    assert "—)" in plot.cursor_readout.toolTip()
    assert plot.copy_coordinates()
    assert qtw.QApplication.clipboard().text().endswith(", —)")
    plot.mouseMoved(plot.vb.mapViewToScene(QtCore.QPointF(0., 3.)))
    assert float(plot.pos_labels["z"].text()) == 430.
    plot.set_plot_axes_swapped(True)
    wait_for(lambda: loaded(plot) and plot.axis_options["x"] == "bias")
    assert "X: bias (V)" in plot.cursor_readout.toolTip()
    assert "Y: gate (V)" in plot.cursor_readout.toolTip()
    plot.mouseMoved(QtCore.QPointF(-1000, -1000))
    assert float(plot.pos_labels["depth"].text()) == plot.slice_frame.depth
    plot.select_slice(0)
    wait_for(lambda: loaded(plot) and plot.slice_frame.index == 0)
    assert float(plot.pos_labels["depth"].text()) == plot.slice_frame.depth
    assert not errors


def test_cursor_fields_stay_left_aligned_and_stationary_across_index_digits(tmp_path, main):
    window, errors = main
    path = tmp_path / "cursor-width.db"
    guid = build_volume(path, gate_points=np.arange(101, dtype=float))
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows))
    plot = window.windows[0]
    wait_for(lambda: loaded(plot) and plot._range_complete)
    index = plot.pos_labels["index"]
    assert index.width() >= index.fontMetrics().horizontalAdvance("[100,1,2]")
    assert index.width() < index.fontMetrics().horizontalAdvance("[1000,10,20]")
    assert all(label.alignment() & QtCore.Qt.AlignmentFlag.AlignLeft for label in plot.pos_labels.values())
    positions = []
    for x in (0., 10., 100., 1.):
        plot.mouseMoved(plot.vb.mapViewToScene(QtCore.QPointF(x, 0.)))
        qtw.QApplication.processEvents()
        assert index.text() == f"[{int(x)},0,0]"
        positions.append(tuple(plot.pos_labels[role].x() for role in ("x", "y", "depth", "z")))
    assert all(position == positions[0] for position in positions)
    plot.select_slice(2)
    wait_for(lambda: loaded(plot) and plot.slice_frame.depth == 4.)
    qtw.QApplication.processEvents()
    assert tuple(plot.pos_labels[role].x() for role in ("x", "y", "depth", "z")) == positions[0]
    assert not errors


def test_keyboard_coordinate_and_mouse_navigation(tmp_path, main):
    window, errors = main
    path = tmp_path / "navigation.db"
    guid = build_volume(path)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows))
    plot = window.windows[0]
    wait_for(lambda: loaded(plot))
    plot.activateWindow()
    plot.setFocus()
    qtw.QApplication.processEvents()
    for key, depth in ((QtCore.Qt.Key.Key_PageDown, 2.5), (QtCore.Qt.Key.Key_End, 4.),
                       (QtCore.Qt.Key.Key_Home, 1.)):
        QtTest.QTest.keyClick(plot, key)
        wait_for(lambda depth=depth: loaded(plot) and plot.slice_frame.depth == depth)
    plot.depth_entry.setFocus()
    plot.depth_entry.setText("2.4")
    QtTest.QTest.keyClick(plot.depth_entry, QtCore.Qt.Key.Key_Return)
    wait_for(lambda: loaded(plot) and plot.slice_frame.depth == 2.5)
    assert plot.depth_entry.text() == "2.5"
    center = QtCore.QPointF(plot.slice_slider.rect().center())
    wheel = QtGui.QWheelEvent(center, center, QtCore.QPoint(), QtCore.QPoint(0, -120),
                             QtCore.Qt.MouseButton.NoButton, QtCore.Qt.KeyboardModifier.NoModifier,
                             QtCore.Qt.ScrollPhase.NoScrollPhase, False)
    qtw.QApplication.sendEvent(plot.slice_slider, wheel)
    wait_for(lambda: loaded(plot) and plot.slice_frame.depth == 1.)
    spin_option = qtw.QStyleOptionSpinBox()
    plot.slice_index.initStyleOption(spin_option)
    up_button = plot.slice_index.style().subControlRect(
        qtw.QStyle.ComplexControl.CC_SpinBox, spin_option,
        qtw.QStyle.SubControl.SC_SpinBoxUp, plot.slice_index)
    QtTest.QTest.mouseClick(plot.slice_index, QtCore.Qt.MouseButton.LeftButton, pos=up_button.center())
    wait_for(lambda: loaded(plot) and plot.slice_frame.depth == 2.5)
    slider = plot.slice_slider
    assert slider.cursor().shape() == QtCore.Qt.CursorShape.OpenHandCursor
    option = qtw.QStyleOptionSlider()
    slider.initStyleOption(option)
    handle = slider.style().subControlRect(
        qtw.QStyle.ComplexControl.CC_Slider, option,
        qtw.QStyle.SubControl.SC_SliderHandle, slider)
    QtTest.QTest.mousePress(slider, QtCore.Qt.MouseButton.LeftButton, pos=handle.center())
    try:
        assert slider.cursor().shape() == QtCore.Qt.CursorShape.ClosedHandCursor
        QtTest.QTest.mouseMove(slider, QtCore.QPoint(slider.width() - 1, handle.center().y()))
        # The image must publish before the mouse button is released.
        wait_for(lambda: loaded(plot) and plot.slice_frame.depth == 4.)
        assert slider.isSliderDown()
        np.testing.assert_array_equal(plot.dataGrid, [[404, 400, 425], [434, 430, 455]])
    finally:
        QtTest.QTest.mouseRelease(slider, QtCore.Qt.MouseButton.LeftButton)
    assert slider.cursor().shape() == QtCore.Qt.CursorShape.OpenHandCursor
    assert not errors


@pytest.mark.parametrize("array", [False, True])
def test_slice_navigation_all_planes_and_global_colours(tmp_path, main, array):
    window, errors = main
    path = tmp_path / "volume.db"
    guid = build_volume(path, array)
    before = trusted_database_artifact_state(path)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows) or bool(errors))
    assert not errors
    plot = window.windows[0]
    assert isinstance(plot, plot3d)
    wait_for(lambda: loaded(plot) or bool(errors))
    assert not errors
    assert plot.axis_options == {"x": "gate", "y": "bias"}
    assert plot._depth_value == 1.
    np.testing.assert_array_equal(plot.dataGrid, [[104, 100, 125], [134, 130, 155]])
    wait_for(lambda: plot._range_complete)
    assert plot.bar.levels() == (100., 455.)
    assert plot.plot_status.message.startswith("Loaded 6 points in ")
    assert "sampled" not in plot.plot_status.message
    assert plot.range_status.text() == ""
    assert plot.slice_status.text() == ""
    assert not plot.volume_status.isVisible()
    assert not hasattr(plot, "heatmap_resolution_label")
    assert plot.heatmap_downsample_button.isHidden()
    for width in (1100, 650):
        plot.resize(width, 560)
        wait_for(lambda: plot.spinBox.mapTo(plot.toolbarRef, QtCore.QPoint()).x()
                 == plot.axis_dropdown["x"].mapTo(plot.slice_toolbar, QtCore.QPoint()).x())
    plot.vb.setRange(xRange=(-1, 2), yRange=(1, 2), padding=0)
    view = plot.vb.viewRange()
    plot.set_marquee_rect(QtCore.QRectF(-3, -1, 4, 3))
    rect = QtCore.QRectF(plot.marquee)
    plot.select_slice(2)
    wait_for(lambda: loaded(plot) and plot._depth_value == 4.)
    assert plot.bar.levels() == (100., 455.)
    np.testing.assert_allclose(plot.vb.viewRange(), view)
    assert plot.marquee == rect
    for x, y in itertools.permutations(("field", "bias", "gate"), 2):
        plot.axis_dropdown["x"].setCurrentText(x)
        plot.axis_dropdown["y"].setCurrentText(y)
        wait_for(lambda x=x, y=y: loaded(plot) and plot.axis_param["x"].name == x and plot.axis_param["y"].name == y)
        assert plot.axis_options == {"x": x, "y": y}
        depth = plot._depth_name()
        index_range = f"Slice index: 0-{len(plot.volume.axes[depth]) - 1}"
        assert plot.slice_slider.toolTip() == index_range
        assert plot.slice_index.toolTip() == index_range
        point = {x: plot.axis_data["x"][0], y: plot.axis_data["y"][0], depth: plot._depth_value}
        assert plot.dataGrid[0, 0] == 100 * point["field"] + 10 * point["bias"] + point["gate"]**2
    assert not errors
    assert_database_artifacts_unchanged_except_trusted_shm(path, before)


def test_cuts_follow_pin_processed_depth_and_csv(tmp_path, main):
    window, errors = main
    path = tmp_path / "volume.db"
    guid = build_volume(path)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows))
    plot = window.windows[0]
    wait_for(lambda: loaded(plot))
    plot._selected_coordinates.update(gate=0., bias=3.)
    plot.openSweep("d")
    wait_for(lambda: len(window.windows) == 2 or bool(errors))
    assert not errors
    depth = window.windows[1]
    wait_for(lambda: loaded(depth) or bool(errors))
    assert not errors
    np.testing.assert_array_equal(depth.axis_data["y"], [130, 280, 430])
    np.testing.assert_array_equal(depth.line.getOriginalDataset()[1], [130, 280, 430])
    plot.oper_widget.restore_state(OperationsState((OperationEntry("row_mean", None),), 1))
    plot.refreshWindow(force=True)
    wait_for(lambda: loaded(plot) and plot._applied_operation_state.entries)
    wait_for(lambda: loaded(depth) and depth.slice_operation_state.entries)
    np.testing.assert_allclose(depth.axis_data["y"], plot.dataGrid[1, 1])
    plot.openSweep("h")
    wait_for(lambda: len(window.windows) == 3 or bool(errors))
    assert not errors
    horizontal = window.windows[2]
    wait_for(lambda: loaded(horizontal))
    plot.select_slice(1)
    wait_for(lambda: loaded(plot) and plot._depth_value == 2.5)
    wait_for(lambda: loaded(horizontal) and horizontal.selection.depth_value == 2.5)
    horizontal.pin_slice.setChecked(True)
    plot.select_slice(2)
    wait_for(lambda: loaded(plot) and plot._depth_value == 4.)
    assert horizontal.selection.depth_value == 2.5
    capture = plot._capture_heatmap_csv()
    target = tmp_path / "slice.csv"
    capture.write(target)
    text = target.read_text()
    assert "field (V)" in text and guid in text and "row_mean" in text
    assert "volume: 1 (signal)" in plot.source_annotation.text
    assert "slice index" not in plot.source_annotation.text
    assert "field =" not in plot.source_annotation.text
    assert not errors


def test_freeze_is_independent_and_supports_operations_and_cuts(tmp_path, main):
    window, errors = main
    path = tmp_path / "freeze.db"
    guid = build_volume(path)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows))
    source = window.windows[0]
    wait_for(lambda: loaded(source))
    source.freeze_slice()
    wait_for(lambda: len(window.windows) == 2 or bool(errors))
    assert not errors
    frozen = window.windows[1]
    wait_for(lambda: loaded(frozen) or bool(errors))
    assert not errors
    original = frozen.dataGrid.copy()
    source.select_slice(2)
    wait_for(lambda: loaded(source) and source._depth_value == 4.)
    np.testing.assert_array_equal(frozen.dataGrid, original)
    frozen.oper_widget.restore_state(OperationsState((OperationEntry("row_mean", None),), 1))
    frozen.refreshWindow(force=True)
    wait_for(lambda: loaded(frozen) and frozen.worker.operations)
    np.testing.assert_allclose(np.asarray(frozen.dataGrid, float).mean(axis=1), 0, atol=1e-12)
    frozen.openSweep("h")
    wait_for(lambda: len(window.windows) == 3 or bool(errors))
    assert not errors
    line = window.windows[2]
    wait_for(lambda: loaded(line))
    np.testing.assert_array_equal(line.axis_data["y"], frozen.dataGrid[1])
    assert "Frozen" in frozen.source_annotation.text
    assert not errors


def test_live_refresh_preserves_coordinate_and_follow_latest_acquisition(tmp_path, main, monkeypatch):
    from tests.datahandling.test_trusted_plot import prohibit_snapshots

    window, errors = main
    path = tmp_path / "live-volume.db"
    initialise_or_create_database_at(str(path), journal_mode="WAL")
    experiment = load_or_create_experiment("live3D", "synthetic")
    measurement = Measurement(exp=experiment)
    for name in ("field", "bias", "gate"):
        measurement.register_custom_parameter(name)
    measurement.register_custom_parameter("signal", setpoints=("field", "bias", "gate"))
    try:
        with measurement.run(write_in_background=False) as saver:
            def add(field, bias, gate, signal):
                saver.add_result(("field", field), ("bias", bias), ("gate", gate), ("signal", signal))
                saver.flush_data_to_database(block=True)
            add(4., 0., 0., 400.)
            add(2., 0., 0., 200.)
            prohibit_snapshots(monkeypatch)
            assert window.load_file(str(path))
            wait_for(lambda: not window._database_load_active)
            window.openPlot(saver.dataset.guid)
            wait_for(lambda: bool(window.windows))
            plot = window.windows[0]
            wait_for(lambda: loaded(plot))
            plot.monitor.stop()
            plot.select_slice(1)
            wait_for(lambda: loaded(plot) and plot._depth_value == 4.)
            plot.setColorbarManualRange(0, 500)
            add(1., 0., 1., 100.)
            before = trusted_database_artifact_state(path)
            plot.load_data()
            wait_for(lambda: loaded(plot) and len(plot.volume.axes["field"]) == 3)
            assert plot._depth_value == 4. and plot._depth_index == 2
            assert plot.bar.levels() == (0., 500.)
            assert plot.slice_status.text() == "1 cell missing"
            assert plot.volume_status.isVisible()
            plot.follow_latest.setChecked(True)
            wait_for(lambda: loaded(plot) and plot._depth_value == 1.)
            assert plot.follow_latest.isChecked()
            plot.mouseMoved(plot.vb.mapViewToScene(QtCore.QPointF(0., 0.)))
            assert plot.pos_labels["z"].text() == "—"
            assert float(plot.pos_labels["depth"].text()) == 1.
            assert_database_artifacts_unchanged_except_trusted_shm(path, before)
            plot._selected_coordinates.update(gate=0., bias=0.)
            plot.openSweep("d")
            wait_for(lambda: len(window.windows) == 2 or bool(errors))
            assert not errors
            cut = window.windows[1]
            wait_for(lambda: loaded(cut))
            assert np.isnan(cut.axis_data["y"][0])
            np.testing.assert_array_equal(cut.axis_data["y"][1:], [200., 400.])
            plot.select_slice(1)
            wait_for(lambda: loaded(plot) and plot._depth_value == 2.)
            assert not plot.follow_latest.isChecked()
            # Retaining a cut keeps its acquisition source working after the
            # original viewer closes, without another database owner.
            plot.close()
            add(2., 0., 0., 300.)
            plot.load_data()
            wait_for(lambda: loaded(cut) and cut.axis_data["y"][1] == 250.)
            assert not errors
        saver.dataset.conn.close()
    finally:
        experiment.conn.close()


def test_slice_colours_axis_swap_and_cut_marker_units(tmp_path, main):
    window, errors = main
    path = tmp_path / "colour.db"
    guid = build_volume(path)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows))
    plot = window.windows[0]
    wait_for(lambda: loaded(plot))
    plot.color_mode.setCurrentIndex(1)
    assert plot.bar.levels() == (100, 155)
    assert plot.range_status.text() == ""
    plot.select_slice(2)
    wait_for(lambda: loaded(plot) and plot._depth_value == 4.)
    assert plot.bar.levels() == (400, 455)
    plot.setColorbarManualRange(50, 500)
    assert plot.range_status.text() == ""
    plot.select_slice(0)
    wait_for(lambda: loaded(plot) and plot._depth_value == 1.)
    assert plot.bar.levels() == (50, 500)
    plot._selected_coordinates.update(gate=0., bias=3.)
    plot.openSweep("h")
    wait_for(lambda: len(window.windows) == 2)
    cut = window.windows[1]
    wait_for(lambda: loaded(cut))
    old_values = cut.axis_data["y"].copy()
    plot.oper_widget.restore_state(OperationsState((OperationEntry("derivative_x", None),), 1))
    plot.refreshWindow(force=True)
    wait_for(lambda: loaded(plot) and plot.display_param.unit == "A/V")
    assert plot._color_mode != "locked"
    wait_for(lambda: loaded(cut) and cut.display_param.unit == "A/V")
    plot.set_plot_axes_swapped(True)
    wait_for(lambda: loaded(plot) and plot.axis_options["x"] == "bias")
    wait_for(lambda: loaded(cut) and cut.selection.kind == "v")
    assert cut.selection.varying == "gate"
    assert cut.marker.isVisible()
    assert plot._applied_operation_state.entries[0].key == "derivative_y"
    assert not np.array_equal(cut.axis_data["y"], old_values)
    assert not errors


def test_new_navigation_cancels_old_worker_and_exports_only_displayed_plane(tmp_path, main, monkeypatch):
    from qplot.tools.volume_worker import VolumeLoader
    from qplot.tools.worker import PlotWorkCancelled

    window, errors = main
    path = tmp_path / "cancel.db"
    guid = build_volume(path)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows))
    plot = window.windows[0]
    wait_for(lambda: loaded(plot) and plot._range_complete)
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    original = VolumeLoader._run
    def delay(worker):
        if worker.depth_index == 1:
            entered.set()
            try:
                while not release.wait(.01):
                    worker._check_cancelled()
            except PlotWorkCancelled:
                cancelled.set()
                worker._finish_cancelled()
                return
        original(worker)
    monkeypatch.setattr(VolumeLoader, "_run", delay)
    try:
        plot.select_slice(1)
        wait_for(entered.is_set)
        # While a new plane is pending, CSV still describes the old displayed
        # samples, not the requested depth in the navigator.
        capture = plot._capture_heatmap_csv()
        assert dict(capture.constants)["field (V)"] == 1.
        assert capture.grid[0, 0] == 104.
        plot.select_slice(2)
        wait_for(lambda: loaded(plot) and plot._depth_value == 4.)
        wait_for(cancelled.is_set)
        assert plot.dataGrid[0, 0] == 404.
        assert dict(plot._capture_heatmap_csv().constants)["field (V)"] == 4.
        assert not errors
    finally:
        release.set()


def test_snapshot_fallback_array_volume_and_region_coverage(tmp_path, main, monkeypatch):
    from pyqtgraph.exporters import CSVExporter

    from tests.windows.test_plot_integration import force_snapshot_fallback

    window, errors = main
    force_snapshot_fallback(monkeypatch)
    path = tmp_path / "array-fallback.db"
    guid = build_volume(path, array=True)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows) or bool(errors))
    assert not errors
    plot = window.windows[0]
    wait_for(lambda: loaded(plot) or bool(errors))
    assert not errors
    plot.set_marquee_rect(QtCore.QRectF(-3., -1., 4., 2.))
    plot.openSweep("mean")
    wait_for(lambda: len(window.windows) == 2 or bool(errors))
    assert not errors
    cut = window.windows[1]
    wait_for(lambda: loaded(cut))
    np.testing.assert_array_equal(cut._coverage_counts, [2, 2, 2])
    np.testing.assert_allclose(cut.axis_data["y"], [102, 252, 402])
    cut.show_coverage()
    capture = cut._capture_line_csv(CSVExporter(cut.plot))
    assert "finite_cells" in capture.headers
    np.testing.assert_array_equal(capture.columns[-1], [2, 2, 2])
    assert not errors
