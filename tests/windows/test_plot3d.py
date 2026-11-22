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
from qplot.windows._widgets.operations import OperationEntry, OperationsState
from qplot.windows.plot3d import plot3d
from tests.support.windows.plot3d import (
    build_mixed_volume as build_mixed_volume,
)
from tests.support.windows.plot3d import (
    build_volume as build_volume,
)
from tests.support.windows.plot3d import (
    loaded as loaded,
)
from tests.support.windows.plot3d import (
    main as main,
)
from tests.support.windows.plot_integration import (
    assert_database_artifacts_unchanged_except_trusted_shm,
    trusted_database_artifact_state,
    wait_for,
)


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
        qtw.QApplication.processEvents()
        assert plot.plot_status.isVisible()
        assert plot.axis_dropdown["x"].isVisible()
        assert not plot.spinBox.isVisible()
        assert not hasattr(plot, "toolbarRef")
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


@pytest.mark.parametrize("edit_reversed", [False, True], ids=["restore-transposed", "restore-last-edit"])
def test_plane_state_follows_physical_axes_on_indirect_return(tmp_path, main, edit_reversed):
    window, errors = main
    path = tmp_path / "plane-settings.db"
    guid = build_volume(path)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows) and loaded(window.windows[0]))
    plot = window.windows[0]

    def edit(mean, limit, rect, ranges):
        entries = (OperationEntry(mean, None), OperationEntry("limit_maximum", limit))
        plot.oper_widget.restore_state(OperationsState(entries, 1))
        old = plot.worker
        plot.oper_widget.apply_but.click()
        wait_for(lambda: plot.worker is not old and loaded(plot))
        plot.vb.setRange(xRange=ranges[0], yRange=ranges[1], padding=0)
        plot.set_marquee_rect(rect)
        return QtCore.QRectF(plot.marquee), plot.vb.viewRange()

    def return_via_other_depth():
        first = plot.axis_options["x"]
        for role, name in (("x", "field"), ("y", "field"), ("y", first)):
            old = plot.worker
            plot.axis_dropdown[role].setCurrentText(name)
            wait_for(lambda old=old: plot.worker is not old and loaded(plot))

    limit = "11"
    rect, ranges = edit("row_mean", limit, QtCore.QRectF(-2.1, -.1, 2.2, .2),
                        [(-2.5, 1.5), (-1., 2.)])
    return_via_other_depth()
    if edit_reversed:
        limit = "6"
        rect, ranges = edit("column_mean", limit, QtCore.QRectF(2.9, 4.9, .2, .2),
                            [(1., 4.), (3., 6.)])
        return_via_other_depth()

    mean = "row_mean" if edit_reversed else "column_mean"
    entries = (OperationEntry(mean, None), OperationEntry("limit_maximum", limit))
    assert plot.oper_widget.capture_state().entries == entries
    assert plot._applied_operation_state.entries == entries
    # The measured signal varies as gate squared at each fixed bias.
    across_gate = np.array([4., 0., 25.]) - 29 / 3
    grid = np.tile(np.minimum(across_gate, float(limit)), (2, 1))
    np.testing.assert_allclose(plot.dataGrid, grid if edit_reversed else grid.T)
    assert plot.marquee == QtCore.QRectF(rect.y(), rect.x(), rect.height(), rect.width())
    np.testing.assert_allclose(plot.vb.viewRange(), ranges[::-1])
    assert not errors


def test_keyboard_cycles_visible_volume_probes_and_moves_selected_probe(tmp_path, main, qapplication):
    window, errors = main
    path = tmp_path / "volume.db"
    guid = build_volume(path)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows))
    plot = window.windows[0]
    wait_for(lambda: loaded(plot))
    plot._selected_coordinates.update(gate=0., bias=0.)
    cuts = []
    for kind in ("h", "h", "v", "d", "mean"):
        if kind == "mean":
            plot.set_marquee_rect(QtCore.QRectF(-2., 0., 7., 3.))
        plot.openSweep(kind)
        wait_for(lambda: len(window.windows) == len(cuts) + 2 or bool(errors))
        assert not errors
        cuts.append(window.windows[-1])
        wait_for(lambda: loaded(cuts[-1]) or bool(errors))
        assert not errors
    first, second, vertical, depth, mean = cuts
    assert mean.marker is None
    assert plot._active_volume_cut is depth
    assert depth.marker.pen.widthF() == 3.
    plot.activateWindow()
    plot.widget.setFocus()
    qapplication.processEvents()
    control = QtCore.Qt.KeyboardModifier.ControlModifier
    backwards = control | QtCore.Qt.KeyboardModifier.ShiftModifier

    def press(key, modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
        QtTest.QTest.keyClick(plot.widget, key, modifiers)

    press(QtCore.Qt.Key.Key_Tab, control)
    assert plot._active_volume_cut is first
    assert first.marker.pen.widthF() == 3.
    assert depth.marker.pen.widthF() == 1.
    press(QtCore.Qt.Key.Key_Up)
    assert first.selection.y_value == 3.
    assert second.selection.y_value == 0.
    press(QtCore.Qt.Key.Key_Tab, control)
    assert plot._active_volume_cut is second
    press(QtCore.Qt.Key.Key_Tab, control)
    assert plot._active_volume_cut is vertical
    press(QtCore.Qt.Key.Key_Right)
    assert vertical.selection.x_value == 5.
    press(QtCore.Qt.Key.Key_Tab, control)
    assert plot._active_volume_cut is depth
    press(QtCore.Qt.Key.Key_Up)
    press(QtCore.Qt.Key.Key_Right)
    assert (depth.selection.x_value, depth.selection.y_value) == (5., 3.)
    assert qapplication.activeWindow() is plot
    assert plot.widget.hasFocus()
    press(QtCore.Qt.Key.Key_Tab, backwards)
    assert plot._active_volume_cut is vertical

    # Pinned cuts on another slice and cuts without a source marker are skipped.
    first.pin_slice.setChecked(True)
    plot.select_slice(1)
    wait_for(lambda: loaded(plot) and plot.slice_frame.index == 1)
    assert not first.marker.isVisible()
    plot.activate_volume_cut(depth)
    press(QtCore.Qt.Key.Key_Tab, control)
    assert plot._active_volume_cut is second
    second.marker.sigClicked.emit(second.marker, None)
    assert plot._active_volume_cut is second
    depth.marker.sigClicked.emit(depth.marker, None)
    assert plot._active_volume_cut is depth
    depth.close()
    press(QtCore.Qt.Key.Key_Tab, backwards)
    assert plot._active_volume_cut is vertical
    assert not errors


@pytest.mark.parametrize("kind", ["h", "v", "d"])
@pytest.mark.parametrize("all_cuts", [False, True])
def test_volume_probe_double_click_closes_profiles_without_opening_appearance(
    tmp_path, main, monkeypatch, kind, all_cuts,
):
    window, errors = main
    path = tmp_path / "volume.db"
    guid = build_volume(path)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows))
    plot = window.windows[0]
    wait_for(lambda: loaded(plot))
    plot._selected_coordinates.update(gate=0., bias=0.)
    plot.openSweep(kind)
    wait_for(lambda: len(window.windows) == 2)
    target = window.windows[-1]
    wait_for(lambda: loaded(target))
    plot._selected_coordinates.update(gate=5., bias=3.)
    plot.openSweep("h")
    wait_for(lambda: len(window.windows) == 3)
    other = window.windows[-1]
    wait_for(lambda: loaded(other))
    opened = []
    monkeypatch.setattr(plot, "open_heatmap_appearance_dialog", opened.append)
    plot.activateWindow()
    plot.widget.setFocus()
    QtTest.QTest.qWait(30)
    x, y = {"h": (1., 0.), "v": (0., 1.5), "d": (0., 0.)}[kind]
    position = plot.widget.mapFromScene(plot.vb.mapViewToScene(QtCore.QPointF(x, y)))
    if kind == "d":
        marker_bounds = target.marker.deviceTransform(plot.widget.viewportTransform()).map(
            target.marker.shape()).boundingRect()
        position = QtCore.QPoint(round(marker_bounds.right() + 4.), round(marker_bounds.center().y()))
    else:
        position += QtCore.QPoint(5, 0) if kind == "v" else QtCore.QPoint(0, 5)
    modifiers = (QtCore.Qt.KeyboardModifier.ShiftModifier if all_cuts
                 else QtCore.Qt.KeyboardModifier.NoModifier)
    viewport = plot.widget.viewport()
    QtTest.QTest.mouseMove(viewport, position)
    QtTest.QTest.mouseClick(viewport, QtCore.Qt.MouseButton.LeftButton, modifiers, position)
    QtTest.QTest.mouseDClick(viewport, QtCore.Qt.MouseButton.LeftButton, modifiers, position)
    QtTest.QTest.mouseRelease(viewport, QtCore.Qt.MouseButton.LeftButton, modifiers, position)
    wait_for(lambda: target._closed or bool(opened))
    assert opened == []
    assert target._closed and target not in plot._cuts
    assert other._closed == all_cuts
    assert len(plot._cuts) == (0 if all_cuts else 1)
    assert not errors


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


def test_depth_marker_tracks_physical_slice_when_cut_axis_is_logarithmic(tmp_path, main):
    window, errors = main
    path = tmp_path / "log-depth-marker.db"
    guid = build_volume(path, gate_points=(-3., 0., 5., 125.))
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows) and loaded(window.windows[0]))
    source = window.windows[0]
    source.axis_dropdown["x"].setCurrentText("field")
    wait_for(lambda: loaded(source) and source._displayed_depth_name() == "gate")
    source.select_slice(2)
    wait_for(lambda: loaded(source) and source.slice_frame.depth == 5.)
    source.openSweep("d")
    wait_for(lambda: len(window.windows) == 2 and loaded(window.windows[1]))
    cut = window.windows[1]
    marker = cut.depth_marker
    assert marker.isVisible() and marker.value() == 5.
    cut.open_axis_scale_dialog("x")
    log = cut._axis_scale_controls["x"].logCheck
    log.click()
    cut._axis_scale_dialog.accept()
    assert marker.isVisible() and marker.value() == pytest.approx(np.log10(5.))

    # The parent navigator still offers zero. Its marker has no log position,
    # but the cut's positive samples remain available for navigation.
    source.select_slice(1)
    wait_for(lambda: loaded(source) and source.slice_frame.depth == 0.)
    assert not marker.isVisible()
    cut.vb.setRange(xRange=(0., 3.), yRange=(0., 17_000.), padding=0.)
    y = cut.line.getOriginalDataset()[1][-1]
    scene = cut.vb.mapViewToScene(QtCore.QPointF(np.log10(125.), y))
    pixel = cut.widget.mapFromScene(scene)
    assert cut.widget.viewport().rect().contains(pixel)
    QtTest.QTest.mouseClick(cut.widget.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=pixel)
    wait_for(lambda: loaded(source) and source.slice_frame.depth == 125.)
    assert marker.isVisible() and marker.value() == pytest.approx(np.log10(125.))

    # A completed marker drag is also expressed in the line's view coordinates.
    marker.setValue(np.log10(4.8))
    marker.sigPositionChangeFinished.emit(marker)
    wait_for(lambda: loaded(source) and source.slice_frame.depth == 5.)
    assert marker.value() == pytest.approx(np.log10(5.))
    cut.open_axis_scale_dialog("x")
    log.click()
    cut._axis_scale_dialog.accept()
    assert marker.value() == 5.
    source.select_slice(0)
    wait_for(lambda: loaded(source) and source.slice_frame.depth == -3.)
    assert marker.isVisible() and marker.value() == -3.
    assert not errors


@pytest.mark.parametrize("kind", ["h", "v", "d", "mean"])
def test_volume_cut_axis_exchange_survives_worker_publication(tmp_path, main, kind):
    from pyqtgraph.exporters import CSVExporter

    window, errors = main
    path = tmp_path / "cut-axes.db"
    guid = build_volume(path)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows) and loaded(window.windows[0]))
    volume = window.windows[0]
    if kind == "mean":
        volume.marquee = QtCore.QRectF(-2., 0., 8., 4.)
    volume.openSweep(kind)
    wait_for(lambda: len(window.windows) == 2 and loaded(window.windows[1]))
    cut = window.windows[1]
    if kind == "h":
        old = cut.worker
        cut.oper_widget.restore_state(OperationsState((OperationEntry("line_derivative", None),), 1))
        cut.refreshWindow(force=True)
        wait_for(lambda: cut.worker is not old and loaded(cut))
        # The writer's signal is a constant plus gate squared.
        np.testing.assert_allclose(cut.line.getOriginalDataset()[1], [-2., 0., 5.], atol=1e-12)
    coordinate, signal = (values.copy() for values in cut.line.getOriginalDataset())
    assert cut.axis_param["x"].name == cut.selection.varying
    assert cut.axis_param["y"].name == "signal"
    for swapped in (True, False):
        old = cut.worker
        assert cut.set_plot_axes_swapped(swapped)
        wait_for(lambda old=old: cut.worker is not old and loaded(cut))
        x, y = cut.line.getOriginalDataset()
        np.testing.assert_array_equal(x, signal if swapped else coordinate)
        np.testing.assert_array_equal(y, coordinate if swapped else signal)
        expected_names = ("signal", cut.selection.varying) if swapped else (cut.selection.varying, "signal")
        assert (cut.axis_param["x"].name, cut.axis_param["y"].name) == expected_names
        assert cut.display_param.unit == cut.axis_param["y"].unit
        capture = cut._capture_line_csv(CSVExporter(cut.plot))
        np.testing.assert_array_equal(capture.columns[0], x)
        np.testing.assert_array_equal(capture.columns[1], y)
        metadata = dict(capture.constants)
        signal_unit = "A/V" if kind == "h" else "A"
        assert (metadata["x_unit"], metadata["value_unit"]) == (
            (signal_unit, "V") if swapped else ("V", signal_unit)
        )
        assert metadata["run_guid"] == guid
        if kind in ("d", "mean"):
            assert cut.depth_marker.angle == (0 if swapped else 90)
            assert cut.depth_marker.value() == volume.slice_frame.depth
        if kind == "mean":
            assert capture.headers[-1] == "finite_cells"
            np.testing.assert_array_equal(capture.columns[-1], [6, 6, 6])
    assert not errors


def test_freeze_is_independent_and_supports_operations_and_cuts(tmp_path, main):
    window, errors = main
    # The plane has six cells; the complete volume has eighteen samples.
    window.config.config["runtime_settings"]["max_full_heatmap_points"] = 6
    path = tmp_path / "freeze.db"
    guid = build_volume(path)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows))
    source = window.windows[0]
    wait_for(lambda: loaded(source) or bool(errors))
    assert not errors
    source.freeze_slice()
    wait_for(lambda: len(window.windows) == 2 or bool(errors))
    assert not errors
    frozen = window.windows[1]
    wait_for(lambda: loaded(frozen) or bool(errors))
    assert not errors
    for plot in (source, frozen):
        assert plot._heatmap_resolution_report is None
        assert plot.heatmap_downsample_button.isHidden()
    original = frozen.dataGrid.copy()
    source.select_slice(2)
    wait_for(lambda: loaded(source) and source._depth_value == 4.)
    np.testing.assert_array_equal(frozen.dataGrid, original)
    capture = frozen._capture_heatmap_csv(layer=frozen)
    np.testing.assert_array_equal(capture.grid, original)
    assert dict(capture.constants)["field (V)"] == 1.
    overlay = SimpleNamespace(
        axis_data=source.axis_data, data_grid=source.dataGrid, from_win=source,
        source_axis_order=lambda: ("x", "y"),
    )
    overlay_capture = frozen._capture_heatmap_csv(layer=overlay)
    np.testing.assert_array_equal(overlay_capture.grid, source.dataGrid)
    assert overlay_capture.constants == ()
    frozen.oper_widget.restore_state(OperationsState((OperationEntry("row_mean", None),), 1))
    frozen.refreshWindow(force=True)
    wait_for(lambda: loaded(frozen) and frozen.worker.operations)
    np.testing.assert_allclose(np.asarray(frozen.dataGrid, float).mean(axis=1), 0, atol=1e-12)
    assert frozen._heatmap_resolution_report is None
    assert frozen.heatmap_downsample_button.isHidden()
    frozen.openSweep("h")
    wait_for(lambda: len(window.windows) == 3 or bool(errors))
    assert not errors
    line = window.windows[2]
    wait_for(lambda: loaded(line))
    np.testing.assert_array_equal(line.axis_data["y"], frozen.dataGrid[1])
    assert "Frozen" in frozen.source_annotation.text
    assert not errors


def test_live_refresh_preserves_coordinate_and_follow_latest_acquisition(tmp_path, main, monkeypatch):
    from tests.support.datahandling.trusted_plot import prohibit_snapshots

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
    plot.vb.setRange(xRange=(-1.25, 1.75), yRange=(3.0, 5.0), padding=0)
    plot.vb.invertX(True)
    plot.set_plot_axes_swapped(True)
    wait_for(lambda: loaded(plot) and plot.axis_options["x"] == "bias")
    wait_for(lambda: loaded(cut) and cut.selection.kind == "v")
    assert cut.selection.varying == "gate"
    assert cut.marker.isVisible()
    assert plot.vb.viewRange() == [[3.0, 5.0], [-1.25, 1.75]]
    assert plot.vb.yInverted() and not plot.vb.xInverted()
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

    from tests.support.windows.plot_integration import force_snapshot_fallback

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
    coverage = next(dialog for dialog in cut.findChildren(qtw.QDialog)
                    if dialog.windowTitle() == "Mean-cut coverage by depth")
    table = coverage.findChild(qtw.QTableWidget)
    assert [table.item(row, 1).text() for row in range(3)] == ["2", "2", "2"]
    assert [table.item(row, 2).text() for row in range(3)] == ["2", "2", "2"]
    assert table.verticalHeader().isHidden()
    assert "Run " in coverage.findChild(qtw.QLabel).text()
    buttons = coverage.findChild(qtw.QDialogButtonBox)
    close = buttons.button(qtw.QDialogButtonBox.StandardButton.Close)
    assert close.isDefault()
    QtTest.QTest.keyClick(close, QtCore.Qt.Key.Key_Return)
    assert not coverage.isVisible()
    capture = cut._capture_line_csv(CSVExporter(cut.plot))
    assert "finite_cells" in capture.headers
    np.testing.assert_array_equal(capture.columns[-1], [2, 2, 2])
    assert not errors


@pytest.mark.parametrize("order", list(itertools.permutations((1, 2, 3))))
def test_mixed_dimensions_any_order(tmp_path, main, monkeypatch, order):
    """Slice controls belong to the destination; ordinary layers stay fixed."""
    from qplot.windows._dragdrop import make_run_preview_mime
    from qplot.windows._plot_drop_axes import DropAxisPlan
    from tests.support.windows.heatmap_layer_integration import _PreviewDropEvent

    window, errors = main
    monkeypatch.setattr(DropAxisPlan, "approve", lambda *args: True)
    path = tmp_path / "mixed-volume.db"
    runs = build_mixed_volume(path)
    artifacts = trusted_database_artifact_state(path)
    window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(runs[order[0]][0])
    wait_for(lambda: bool(window.windows))
    target = window.windows[-1]
    wait_for(lambda: loaded(target))
    # A run can contain measurements of different dimensionalities. The Add
    # picker must use each measurement's dimensions, not their union of axes.
    with monkeypatch.context() as patch:
        patch.setattr(window.RunList, "all_run_metadata", lambda: {99: {
            "guid": runs[3][0], "sweep_parameters": ["field", "bias", "gate", "time"],
            "measure_parameters": ["line", "map", "volume", "unsupported"],
            "preview_dimensions": [1, 2, 3, 4],
        }})
        assert [key.parameter_name for _, key in window._trace_candidates_for_plot(target)] == ["line"]
        expected = ["map"] if isinstance(target, plot3d) else ["map", "volume"]
        assert [key.parameter_name for _, key in window._heatmap_candidates_for_plot(target)] == expected
    for count, dimension in enumerate(order[1:], start=2):
        guid, name = runs[dimension]
        axes = {1: ["gate"], 2: ["bias", "gate"], 3: ["field", "bias", "gate"]}[dimension]
        event = _PreviewDropEvent(make_run_preview_mime(guid, name, axes))
        assert target._handle_preview_drag_drop(event)
        wait_for(lambda count=count: len(window.windows) == 1 and len(window.windows[0]._plot_item_order) == count)
        target = window.windows[0]
        wait_for(lambda target=target: loaded(target))
    assert isinstance(target, plot3d)
    assert target.slice_toolbar.isVisible()
    wait_for(lambda: all(layer is target or layer.geometry is not None for layer in target.heatmaps.values()))
    trace = next(iter(target.lines.values()))
    heatmap = next(layer for layer in target.heatmaps.values() if layer is not target)
    capture = target._capture_heatmap_csv(layer=target)
    np.testing.assert_array_equal(capture.grid, target.dataGrid)
    assert dict(capture.constants)["field (V)"] == target.slice_frame.depth
    overlay_capture = target._capture_heatmap_csv(layer=heatmap)
    np.testing.assert_array_equal(overlay_capture.grid, heatmap.data_grid)
    assert overlay_capture.constants == ()
    wait_for(lambda: trace.getData()[0] is not None)
    wait_for(lambda: target._range_complete)
    assert target.bar.levels() == (0., 455.)
    line_before = tuple(np.copy(values) for values in trace.getData())
    heatmap_before = heatmap.data_grid.copy()
    target.select_slice(1)
    wait_for(lambda: loaded(target) and target.slice_frame.index == 1)
    for before, after in zip(line_before, trace.getData(), strict=True):
        np.testing.assert_array_equal(before, after)
    np.testing.assert_array_equal(heatmap.data_grid, heatmap_before)
    target.oper_widget.restore_state(OperationsState((OperationEntry("row_mean", None),), 1))
    target.refreshWindow(force=True)
    wait_for(lambda: loaded(target) and target._applied_operation_state.entries)
    # Swapping the selected plane axes must not transpose ordinary overlays.
    target.axis_dropdown["x"].setCurrentText(target.axis_options["y"])
    wait_for(lambda: loaded(target) and target.axis_param["x"].name == "bias")
    for before, after in zip(line_before, trace.getData(), strict=True):
        np.testing.assert_array_equal(before, after)
    np.testing.assert_array_equal(heatmap.data_grid, heatmap_before)
    # Reject another volume without creating a source or altering the window.
    assert not window.add_trace_to_plot(target, *runs[3], axes_from_drop=True)
    assert window.windows == [target]
    assert len(target._plot_item_order) == 3
    # A separate viewer of the same volume retains its own navigation.
    window.openWin(plot3d, target._dataset_key, target.param)
    independent = window.windows[-1]
    wait_for(lambda: loaded(independent))
    independent.select_slice(2)
    wait_for(lambda: loaded(independent) and independent.slice_frame.index == 2)
    assert target.slice_frame.index == 1
    independent.close()
    # Removing the volume keeps both ordinary datasets and removes its controls.
    target.remove_plot_items([("heatmap", target._primary_heatmap_key)])
    wait_for(lambda: len(window.windows) == 1 and not isinstance(window.windows[0], plot3d))
    replacement = window.windows[0]
    wait_for(lambda: loaded(replacement))
    assert len(replacement._plot_item_order) == 2
    assert not hasattr(replacement, "slice_toolbar")
    assert_database_artifacts_unchanged_except_trusted_shm(path, artifacts)
    assert errors == []


@pytest.mark.parametrize("dimension", [1, 2])
@pytest.mark.parametrize("failure", ["cancel", "restore"])
def test_adding_volume_rolls_back_without_losing_existing_plot(tmp_path, main, monkeypatch, dimension, failure):
    from qplot.windows import _plot_source_promotion as promotion
    from qplot.windows._plot_drop_axes import DropAxisPlan

    window, errors = main
    path = tmp_path / "rollback-volume.db"
    runs = build_mixed_volume(path)
    window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(runs[dimension][0])
    wait_for(lambda: bool(window.windows))
    target = window.windows[0]
    wait_for(lambda: loaded(target))
    original_items = list(target._plot_item_order)
    key = window._current_dataset_key(runs[3][0])
    # Resolve broker metadata via a normal viewer before exercising the rebuild.
    window.openPlot(runs[3][0])
    wait_for(lambda: len(window.windows) == 2)
    source = window.windows[-1]
    wait_for(lambda: loaded(source))
    param = source.param
    source.close()
    monkeypatch.setattr(DropAxisPlan, "approve", lambda *args: failure != "cancel")
    if failure == "restore":
        def fail_restore(*args):
            raise RuntimeError("injected display restore failure")
        monkeypatch.setattr(promotion, "_restore_display", fail_restore)
        with pytest.raises(RuntimeError, match="injected display restore"):
            promotion.promote_plot_source(window, target, [], volume=(key, param))
    else:
        assert promotion.promote_plot_source(window, target, [], volume=(key, param)) is None
    assert window.windows == [target]
    assert target.isVisible()
    assert target._plot_item_order == original_items
    assert target._merged_trace_users == 0
    assert errors == []


@pytest.mark.parametrize("primary_dimension", [1, 2])
def test_group_drop_follows_volume_promotion_for_remaining_measurements(tmp_path, main, monkeypatch,
                                                                      primary_dimension):
    from qplot.windows._dragdrop import make_run_previews_mime
    from qplot.windows._plot_drop_axes import DropAxisPlan
    from tests.support.windows.heatmap_layer_integration import _PreviewDropEvent

    window, errors = main
    monkeypatch.setattr(DropAxisPlan, "approve", lambda *args: True)
    path = tmp_path / "group-volume.db"
    runs = build_mixed_volume(path)
    before = trusted_database_artifact_state(path)
    window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(runs[primary_dimension][0])
    wait_for(lambda: len(window.windows) == 1 and loaded(window.windows[0]))
    original = window.windows[0]
    other_dimension = 2 if primary_dimension == 1 else 1
    event = _PreviewDropEvent(make_run_previews_mime([
        {"guid": runs[dimension][0], "parameter": runs[dimension][1], "axes_pending": True}
        for dimension in (3, other_dimension)
    ]))
    assert original._handle_preview_drag_drop(event)
    wait_for(lambda: len(window.windows) == 1 and isinstance(window.windows[0], plot3d)
             and len(window.windows[0]._plot_item_order) == 3 and loaded(window.windows[0]))
    replacement = window.windows[0]
    assert replacement is not original
    assert original._preview_drop_replacement() is replacement
    assert len(replacement.lines) == 1
    assert len(replacement.heatmaps) == 2
    wait_for(lambda: all(layer is replacement or layer.geometry is not None
                        for layer in replacement.heatmaps.values()))
    assert errors == []
    assert_database_artifacts_unchanged_except_trusted_shm(path, before)


def test_adding_volume_preserves_manual_colour_limits(tmp_path, main, monkeypatch):
    from qplot.windows._plot_drop_axes import DropAxisPlan

    window, errors = main
    monkeypatch.setattr(DropAxisPlan, "approve", lambda *args: True)
    path = tmp_path / "locked-mixed-volume.db"
    runs = build_mixed_volume(path)
    window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(runs[2][0])
    wait_for(lambda: bool(window.windows))
    original = window.windows[0]
    wait_for(lambda: loaded(original))
    original.setColorbarManualRange(-10, 500)
    window.add_trace_to_plot(original, *runs[3], axes_from_drop=True)
    wait_for(lambda: len(window.windows) == 1 and isinstance(window.windows[0], plot3d))
    target = window.windows[0]
    wait_for(lambda: loaded(target))
    assert target._color_mode == "locked"
    assert target.bar.levels() == (-10., 500.)
    target.select_slice(2)
    wait_for(lambda: loaded(target) and target.slice_frame.index == 2)
    assert target.bar.levels() == (-10., 500.)
    assert errors == []
