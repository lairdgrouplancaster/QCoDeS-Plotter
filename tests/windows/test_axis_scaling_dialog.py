"""Identify and link real QCoDeS plot axes through the scaling dialog."""

from weakref import ref

import pytest
from PyQt6 import QtCore, sip
from PyQt6 import QtWidgets as qtw
from PyQt6.QtTest import QTest

from tests.support.windows.axis_limit_precision import AxisLimitWindow
from tests.support.windows.mixed_plot_drops import _added, _drop
from tests.support.windows.mixed_plot_drops import mixed_plot as mixed_plot
from tests.support.windows.plot_dialog_lifecycle import dialog_plot as dialog_plot
from tests.support.windows.plot_integration import wait_for
from tests.support.windows.sql_heatmap import idle, sql_heatmap, subset, view


def test_use_data_extrema_are_sample_coordinates_on_lines_and_heatmaps(mixed_plot):
    _main, plot, _runs = mixed_plot
    plot.open_axis_scale_dialog("x")
    entry = plot._axis_scale_controls["x"].rangeEntry
    assert entry.auto_radio.isChecked()
    assert all(not button.isChecked() for button in entry.locks.values())
    entry.use_buttons["min"].click()
    assert entry.manual_radio.isChecked()
    assert entry.state.limits[0] == 0
    entry.use_buttons["max"].click()
    assert entry.state.limits == (0., 2.)
    entry.locks["min"].click()
    entry.fit_button.click()
    assert entry.state.limits[0] == 0 and entry.state.limits[1] > 2
    assert entry.manual_radio.isChecked()
    assert entry.locks["min"].isChecked()


@pytest.mark.parametrize("mixed_plot", ["line"], indirect=True)
def test_held_centre_tracks_line_updates_and_unlocked_edit_freezes(mixed_plot):
    _main, plot, _runs = mixed_plot
    plot.open_axis_scale_dialog("x")
    entry = plot._axis_scale_controls["x"].rangeEntry
    entry.auto_radio.click()
    entry.locks["centre"].click()
    field = entry.fields["centre"]
    field.selectAll()
    QTest.keyClicks(field, "3")
    QTest.keyClick(field, QtCore.Qt.Key.Key_Return)
    assert entry.auto_radio.isChecked() and entry.state.held == 3
    plot.line.setData([0, 10], [2, 4])
    assert sum(entry.state.limits) / 2 == pytest.approx(3.)
    assert entry.state.limits[1] >= 10
    field = entry.fields["span"]
    field.selectAll()
    QTest.keyClicks(field, "4")
    assert entry.manual_radio.isChecked()
    QTest.keyClick(field, QtCore.Qt.Key.Key_Return)
    assert entry.state.limits == (1., 5.)
    plot.line.setData([-100, 200], [2, 4])
    assert entry.state.limits == (1., 5.)


def test_use_data_recovers_full_horizontal_extent_from_zoomed_sql_heatmap(tmp_path, monkeypatch):
    with sql_heatmap(tmp_path, monkeypatch, show=True) as (_window, plot):
        full = plot._viewport_state.full
        zoom = subset(full, .25, .75)
        view(plot, zoom)
        plot._reload_visible_heatmap_data()
        idle(plot)
        assert plot._viewport_state.displayed.matches(zoom)
        plot.open_axis_scale_dialog("x")
        entry = plot._axis_scale_controls["x"].rangeEntry
        entry.use_buttons["min"].click()
        entry.use_buttons["max"].click()
        assert entry.state.limits == pytest.approx(full.x)
        assert entry.manual_radio.isChecked()


def test_swap_transposes_zoom_before_the_axes_page_is_opened(mixed_plot):
    _main, plot, _runs = mixed_plot
    plot.vb.setRange(xRange=(1.25, 1.75), yRange=(10, 12), padding=0)
    plot.vb.invertX(True)
    dialog = plot.open_plot_appearance_dialog()
    assert not plot._axis_scale_controls
    dialog.swap_axes.click()
    wait_for(lambda: not plot.worker.running)
    assert plot.vb.viewRange() == [[10, 12], [1.25, 1.75]]
    assert plot.vb.yInverted() and not plot.vb.xInverted()
    # Edit the old X quantity in its new orientation, then cancel both edits.
    plot.open_axis_scale_dialog("y")
    plot._axis_scale_controls["y"].invertCheck.click()
    dialog.reject()
    wait_for(lambda: not plot.worker.running)
    assert plot.vb.viewRange() == [[1.25, 1.75], [10, 12]]
    assert plot.vb.xInverted() and not plot.vb.yInverted()

    # Rapid swaps can share a pending data refresh. Carry the latest zoom.
    assert plot.set_plot_axes_swapped(True)
    plot.vb.setYRange(1.4, 1.6, padding=0)
    assert plot.set_plot_axes_swapped(False)
    wait_for(lambda: not plot.worker.running and plot.axis_param["x"].name == plot.axis_options["x"])
    assert plot.vb.viewRange() == [[1.4, 1.6], [10, 12]]


def test_swap_moves_primary_and_secondary_scale_behaviour(mixed_plot):
    _main, plot, runs = mixed_plot
    _drop(plot, runs["other_line"])
    _added(plot, runs["other_line"])
    plot.open_axis_scale_dialog("x2")
    dialog = plot._plot_appearance_dialog
    top = plot._axis_scale_controls["x2"]
    top.logCheck.click()
    plot._apply_axis_scale_manual_limits("x2", (-1.5, 0.5))
    top.invertCheck.click()
    top.mouseCheck.click()
    left = plot._axis_scale_controls["y"]
    left.autoRadio.click()
    left.rangeEntry.locks["span"].click()
    left.visibleOnlyCheck.click()
    dialog.accept()
    left_state = plot._capture_axis_settings("y")
    top_state = plot._capture_axis_settings("x2")

    dialog = plot.open_plot_appearance_dialog()
    dialog.swap_axes.click()
    wait_for(lambda: not plot.worker.running)
    assert plot._capture_axis_settings("y2") == top_state
    bottom = plot._capture_axis_settings("x")
    assert bottom.filtered_auto and bottom.visible_only
    assert bottom.editor.lock == "span" and bottom.editor.held == left_state.editor.held
    assert bottom.limits[1] - bottom.limits[0] == pytest.approx(left_state.limits[1] - left_state.limits[0])
    assert not plot._axis_scale_log_mode("x2")
    dialog.accept()
    assert plot.set_plot_axes_swapped(False)
    wait_for(lambda: not plot.worker.running)
    assert plot._capture_axis_settings("x2") == top_state
    assert plot._capture_axis_settings("y").filtered_auto


@pytest.mark.parametrize("mixed_plot", ["line"], indirect=True)
def test_swap_moves_a_lone_secondary_scale_to_its_new_view(mixed_plot):
    _main, plot, _runs = mixed_plot
    dialog = plot.open_plot_appearance_dialog()
    dialog.x_axis.setCurrentText("Top")
    dialog.accept()
    assert plot.right_vb is None
    plot.top_vb.setXRange(11, 19, padding=0)
    plot.vb.setYRange(-4, -2, padding=0)
    plot.set_plot_axes_swapped(True)
    wait_for(lambda: not plot.worker.running)
    assert plot.right_vb.viewRange()[1] == [11, 19]
    assert plot.vb.viewRange()[0] == [-4, -2]
    plot.set_plot_axes_swapped(False)
    wait_for(lambda: not plot.worker.running)
    assert plot.top_vb.viewRange()[0] == [11, 19]
    assert plot.vb.viewRange()[1] == [-4, -2]


@pytest.mark.parametrize("mixed_plot", ["line"], indirect=True)
def test_range_link_tracks_both_plots_through_swaps(mixed_plot):
    main, target, runs = mixed_plot
    _drop(target, runs["other_line"])
    _added(target, runs["other_line"])
    main.openPlot(guid=runs["third_line"].guid)
    wait_for(lambda: main.windows[-1].ds.guid == runs["third_line"].guid and not main.windows[-1].worker.running)
    source = main.windows[-1]
    source.open_axis_scale_dialog("x")
    x = source._axis_scale_controls["x"].linkCombo
    y = source._axis_scale_controls["y"].linkCombo
    x.setCurrentIndex(x.findData(ref(target.top_vb)))
    assert source.vb.linkedView(0) is target.top_vb
    source._plot_appearance_dialog.accept()

    def swap(plot, checked):
        assert plot.set_plot_axes_swapped(checked)
        wait_for(lambda: not plot.worker.running)

    swap(source, True)
    assert source.vb.linkedView(0) is source.vb.linkedView(1) is None
    assert "Paused" in y.currentText() and target.windowTitle() in y.currentText()
    paused_range = tuple(source.vb.viewRange()[1])
    target.top_vb.setXRange(20, 30, padding=0)
    assert tuple(source.vb.viewRange()[1]) == paused_range
    swap(target, True)
    assert source.vb.linkedView(1) is target.right_vb
    linked_range = tuple(source.vb.viewRange()[1])
    target.right_vb.setYRange(50, 70, padding=0)
    assert tuple(source.vb.viewRange()[1]) != linked_range
    swap(source, False)
    assert source.vb.linkedView(0) is None and "Paused" in x.currentText()
    swap(target, False)
    assert source.vb.linkedView(0) is target.top_vb
    swap(target, True)
    assert "Paused" in x.currentText()
    source.open_axis_scale_dialog("x")
    x.setCurrentIndex(0)
    source._plot_appearance_dialog.reject()
    assert "Paused" in x.currentText()
    x.setCurrentIndex(0)
    swap(target, False)
    assert source.vb.linkedView(0) is None and x.currentText() == "None"
    x.setCurrentIndex(x.findData(ref(target.top_vb)))
    swap(target, True)
    target.close()
    assert source.vb.linkedView(0) is None and x.currentText() == "None"


def test_link_targets_include_primary_and_secondary_plot_quantities(mixed_plot, qapplication):
    _main, target, runs = mixed_plot
    _drop(target, runs["other_line"])
    _added(target, runs["other_line"])
    source = AxisLimitWindow()
    source.plot.plot([1, 2, 3], [2, 4, 6])
    source.resize(800, 600)
    source.show()
    try:
        qapplication.processEvents()
        for axis, secondary, side, quantity in (
            ("x", "x2", "Top", "u (K)"),
            ("y", "y2", "Right", "w (Hz)"),
        ):
            source.open_axis_scale_dialog(axis)
            combo = source._axis_scale_controls[axis].linkCombo
            assert combo.findData(ref(target.vb)) > 0
            target_view = target._axis_scale_viewbox(secondary)
            index = combo.findData(ref(target_view))
            assert index > 0
            assert combo.itemText(index) == f"{target.windowTitle()} — {side}: {quantity}"
            combo.setCurrentIndex(index)
            number = source._axis_scale_axis_number(axis)
            assert source.vb.linkedView(number) is target_view
            target.open_axis_scale_dialog(secondary)
            tabs = target._axis_scale_tabs
            selected = tabs.currentIndex()
            assert tabs.tabData(selected) == secondary
            assert tabs.tabText(selected) == side
            assert target._axis_scale_quantity.text() == quantity
            direction = "horizontal" if axis == "x" else "vertical"
            assert f"{side} {direction} axis" in tabs.tabToolTip(selected)
            assert quantity in tabs.tabToolTip(selected)
            assert quantity in tabs.accessibleTabName(selected)
            source._sync_axis_scale_controls(axis)
            assert combo.currentData()() is target_view
            combo.setCurrentIndex(0)
            assert source.vb.linkedView(number) is None
    finally:
        source._axis_scale_dialog.close()
        source.close()
        source.deleteLater()
        qtw.QApplication.processEvents()


@pytest.mark.parametrize("dialog_plot", ["line"], indirect=True)
@pytest.mark.parametrize(("axis", "linked", "retain"), [
    pytest.param("x", True, False, id="linked-target-retires"),
    pytest.param("y", True, True, id="target-retained-as-overlay"),
    pytest.param("x", False, False, id="unselected-target-retires"),
])
def test_closed_target_leaves_axis_independent_and_updates_open_chooser(
    dialog_plot, axis, linked, retain,
):
    main, receiver, app, guids = dialog_plot
    main.openPlot(guid=guids[1])
    wait_for(lambda: len(main.windows) == 2)
    target = main.windows[-1]
    wait_for(lambda: hasattr(target, "axis_data") and not target.worker.running)
    if retain:
        assert main.add_trace_to_plot(
            receiver, target._dataset_key, target.param.name, param=target.param,
        )
        wait_for(lambda: len(receiver.lines) == 2)
    receiver.open_axis_scale_dialog(axis)
    controls = receiver._axis_scale_controls[axis]
    combo = controls.linkCombo
    index = combo.findData(ref(target.vb))
    assert index > 0
    label = combo.itemText(index)
    dimension = receiver._axis_scale_axis_number(axis)
    if linked:
        combo.setCurrentIndex(index)
        assert receiver.vb.linkedView(dimension) is target.vb
    controls.minText.setText("-20.25")  # An uncommitted edit in the open dialogue.
    target.close()
    assert target not in main.windows
    assert receiver._axis_scale_dialog.isVisible()
    assert combo.currentText() == "None"
    assert combo.findText(label) == -1
    assert receiver.vb.linkedView(dimension) is None
    assert controls.minText.text() == "-20.25"
    if retain:
        assert not sip.isdeleted(target) and target._merged_trace_users == 1
    else:
        def retired():
            app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            return sip.isdeleted(target)

        wait_for(retired)
