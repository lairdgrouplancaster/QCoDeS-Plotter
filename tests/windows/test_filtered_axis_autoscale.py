"""Exercise autoscale options in loaded qPlot line windows, with linked axes."""

import numpy as np
import pytest
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from PyQt6.QtTest import QTest

from tests.windows import test_plot_overlay_integration as overlays
from tests.windows.test_plot_integration import wait_for

loaded_plot = overlays.loaded_plot


def click(widget):
    QTest.mouseClick(
        widget, QtCore.Qt.MouseButton.LeftButton,
        pos=QtCore.QPoint(8, widget.height() // 2),
    )
    qtw.QApplication.processEvents()


def reopen(window, axis):
    window._axis_scale_dialog.close()
    window.open_axis_scale_dialog(axis)
    qtw.QApplication.processEvents()


def assert_data_bounds(bounds, expected):
    # The curve's cosmetic pen adds a few pixels to childrenBounds, in addition
    # to normal autoscale padding. Check the data center and a tight span bound
    # without duplicating qPlot's bounds calculation in the test.
    assert np.mean(bounds) == pytest.approx(np.mean(expected))
    assert np.ptp(expected) <= np.ptp(bounds) <= 1.1 * np.ptp(expected)


def edit_limits(window, axis, limits):
    window.open_axis_scale_dialog(axis)
    controls = window._axis_scale_controls[axis]
    controls.minText.setText(str(limits[0]))
    controls.maxText.setText(str(limits[1]))
    QTest.keyClick(controls.maxText, QtCore.Qt.Key.Key_Return)
    qtw.QApplication.processEvents()


@pytest.mark.parametrize("loaded_plot", [True], indirect=True)
@pytest.mark.parametrize("sides", [
    ("Bottom", "Left"), ("Bottom", "Right"),
    ("Top", "Left"), ("Top", "Right"),
])
@pytest.mark.parametrize("dimension", [0, 1])
@pytest.mark.parametrize("change", ["dialog", "pan", "zoom", "linked"])
def test_visible_auto_follows_perpendicular_range(loaded_plot, sides, dimension, change):
    window = loaded_plot
    window._ensure_trace_axis_viewboxes(top=True, right=True)
    owner = overlays._assign_trace_axes(window, window.label, *sides)
    other_key = next(key for key in window.lines if key != window.label)
    opposite = (
        "Top" if sides[0] == "Bottom" else "Bottom",
        "Right" if sides[1] == "Left" else "Left",
    )
    overlays._assign_trace_axes(window, other_key, *opposite)
    for vb in (window.vb, window.top_vb, window.right_vb, window.top_right_vb):
        vb.enableAutoRange(enable=False)
    window._axis_scale_custom_auto_axes.clear()
    x_axis = "x" if sides[0] == "Bottom" else "x2"
    y_axis = "y" if sides[1] == "Left" else "y2"
    auto_axis, perpendicular = (y_axis, x_axis) if dimension == 0 else (x_axis, y_axis)
    initial = [0, 10] if dimension == 0 else [10, 20]
    target = [50, 63] if dimension == 0 else [60, 73]
    edit_limits(window, perpendicular, initial)
    window.open_axis_scale_dialog(auto_axis)
    controls = window._axis_scale_controls[auto_axis]
    click(controls.visibleOnlyCheck)
    click(controls.autoRadio)
    auto_vb = window._axis_scale_viewbox(auto_axis)
    assert_data_bounds(auto_vb.viewRange()[1 - dimension], [10, 20] if dimension == 0 else [0, 10])
    unrelated = {
        axis: window._axis_scale_viewbox(axis).viewRange()[number][:]
        for axis, number in (("x", 0), ("y", 1), ("x2", 0), ("y2", 1))
        if axis not in (auto_axis, perpendicular)
    }
    worker = window.worker
    before_data = tuple(values.copy() for values in window.line.getData())

    if change == "dialog":
        edit_limits(window, perpendicular, target)
    elif change == "linked":
        # Link through the actual dialog to the other QCoDeS plot's named view.
        source = window.lines[other_key].from_win
        source.vb.register("Visible autoscale source")
        source.show()
        source.resize(window.size())
        source.move(window.pos())
        qtw.QApplication.processEvents()
        source.vb.enableAutoRange(enable=False)
        window.open_axis_scale_dialog(perpendicular)
        combo = window._axis_scale_controls[perpendicular].linkCombo
        index = combo.findText(source.vb.name)
        assert index >= 0
        combo.setCurrentIndex(index)
        assert window._axis_scale_viewbox(perpendicular).linkedView(dimension) is source.vb
        source.vb.setRange(**{"xRange" if dimension == 0 else "yRange": target}, padding=0)
        assert source.vb.viewRange()[dimension] == target
    else:
        # Use ViewBox's pan/zoom entry points; no refresh or Auto click follows.
        kwargs = {"x" if dimension == 0 else "y": 50}
        owner.translateBy(**kwargs)
        factor = 1.3 if change == "zoom" else 1.0
        owner.scaleBy(**{"x" if dimension == 0 else "y": factor},
                      center=QtCore.QPointF(50, 60))
        if change == "pan":
            target = [50, 60] if dimension == 0 else [60, 70]
    qtw.QApplication.processEvents()

    expected = np.array(target) + (10 if dimension == 0 else -10)
    if change == "linked":
        # Native linking aligns screen coordinates when plot margins differ.
        visible = owner.viewRange()[dimension]
        data = window.line.getData()
        selected = data[1 - dimension][
            (data[dimension] >= visible[0]) & (data[dimension] <= visible[1])
        ]
        assert selected.size >= 2
        expected = [selected.min(), selected.max()]
        assert expected[0] > (20 if dimension == 0 else 10)
    assert_data_bounds(auto_vb.viewRange()[1 - dimension], expected)
    assert auto_axis in window._axis_scale_custom_auto_axes
    reopen(window, auto_axis)
    assert controls.autoRadio.isChecked()
    assert controls.visibleOnlyCheck.isChecked()
    for axis, bounds in unrelated.items():
        np.testing.assert_allclose(window._axis_scale_viewbox(axis).viewRange()[window._axis_scale_axis_number(axis)], bounds)
    assert window.worker is worker and not worker.running
    for actual, before in zip(window.line.getData(), before_data, strict=True):
        np.testing.assert_array_equal(actual, before)

    # Selecting Manual cancels only this axis's Auto, leaving its range intact.
    click(controls.manualRadio)
    manual = auto_vb.viewRange()[1 - dimension][:]
    edit_limits(window, perpendicular, initial)
    np.testing.assert_array_equal(auto_vb.viewRange()[1 - dimension], manual)
    assert controls.manualRadio.isChecked()
    assert controls.visibleOnlyCheck.isChecked()


@pytest.mark.parametrize("loaded_plot", [True], indirect=True)
@pytest.mark.parametrize("auto_pan", [False, True])
def test_multiple_visible_auto_axes_keep_options_and_empty_regions(loaded_plot, auto_pan):
    window = loaded_plot
    window._ensure_trace_axis_viewboxes(top=True, right=True)
    other_key = next(key for key in window.lines if key != window.label)
    overlays._assign_trace_axes(window, other_key, "Bottom", "Right")
    for vb in (window.vb, window.top_vb, window.right_vb, window.top_right_vb):
        vb.enableAutoRange(enable=False)
    window._axis_scale_custom_auto_axes.clear()
    edit_limits(window, "x", [0, 10])
    for axis in ("y", "y2"):
        window.open_axis_scale_dialog(axis)
        controls = window._axis_scale_controls[axis]
        click(controls.visibleOnlyCheck)
        controls.autoPercentSpin.setValue(60)
        if auto_pan:
            click(controls.autoPanCheck)
    spans = {
        axis: np.ptp(window._axis_scale_viewbox(axis).viewRange()[1])
        for axis in ("y", "y2")
    }
    edit_limits(window, "x", [30, 40])
    for axis in ("y", "y2"):
        bounds = window._axis_scale_viewbox(axis).viewRange()[1]
        assert np.mean(bounds) == pytest.approx(45)
        if auto_pan:
            assert np.ptp(bounds) == pytest.approx(spans[axis])
        else:
            assert_data_bounds(bounds, [42, 48])
        reopen(window, axis)
        controls = window._axis_scale_controls[axis]
        assert controls.autoRadio.isChecked()
        assert controls.visibleOnlyCheck.isChecked()
        assert controls.autoPercentSpin.value() == 60
        assert controls.autoPanCheck.isChecked() is auto_pan

    # Only the 64-point main trace has samples here. The 48-point right trace
    # retains its Auto mode and last usable range until data is visible again.
    right_before = window.right_vb.viewRange()[1][:]
    edit_limits(window, "x", [50, 63])
    assert np.mean(window.vb.viewRange()[1]) == pytest.approx(66.5)
    np.testing.assert_array_equal(window.right_vb.viewRange()[1], right_before)
    assert window._axis_scale_custom_auto_axes == {"y", "y2"}
    edit_limits(window, "x", [0, 10])
    assert np.mean(window.right_vb.viewRange()[1]) == pytest.approx(15)


def test_mutually_dependent_visible_auto_axes_settle_without_feedback(loaded_plot):
    window = loaded_plot
    overlays._assign_trace_axes(window, window.label, "Bottom", "Right")
    window.vb.enableAutoRange(enable=False)
    window.right_vb.enableAutoRange(enable=False)
    window._axis_scale_custom_auto_axes.clear()
    edit_limits(window, "x", [0, 63])
    edit_limits(window, "y2", [10, 73])
    for axis in ("x", "y2"):
        window.open_axis_scale_dialog(axis)
        controls = window._axis_scale_controls[axis]
        click(controls.visibleOnlyCheck)
        click(controls.autoRadio)
    # These controls own different ViewBoxes, so both visible-only options can
    # be active even though the assigned trace's axes depend on one another.
    changes = []
    window.vb.sigXRangeChanged.connect(lambda *_: changes.append("x"))
    window.right_vb.sigYRangeChanged.connect(lambda *_: changes.append("y2"))
    window.open_axis_scale_dialog("x")
    window._axis_scale_controls["x"].autoPercentSpin.setValue(60)
    qtw.QApplication.processEvents()
    assert_data_bounds(window.vb.viewRange()[0], [12.6, 50.4])
    # Native linking aligns screen coordinates, so the trace's owning view
    # can expose a different edge sample when platform margins differ.
    x, y = window.line.getData()
    visible = window.line.getViewBox().viewRange()[0]
    selected = y[(x >= visible[0]) & (x <= visible[1])]
    assert selected.size >= 2
    assert_data_bounds(window.right_vb.viewRange()[1], [selected.min(), selected.max()])
    assert 1 <= len(changes) <= 2
    settled = window.vb.viewRange()[0][:], window.right_vb.viewRange()[1][:]
    count = len(changes)
    # Give queued Qt timers and repaints several opportunities to expose loops.
    for _ in range(10):
        QTest.qWait(10)
    assert len(changes) == count
    np.testing.assert_array_equal(window.vb.viewRange()[0], settled[0])
    np.testing.assert_array_equal(window.right_vb.viewRange()[1], settled[1])
    assert not window._axis_scale_visible_refresh_timer.isActive()
    assert window._axis_scale_custom_auto_axes == {"x", "y2"}
    for axis in ("x", "y2"):
        reopen(window, axis)
        assert window._axis_scale_controls[axis].autoRadio.isChecked()
        assert window._axis_scale_controls[axis].visibleOnlyCheck.isChecked()


@pytest.mark.parametrize("gesture", ["pan", "zoom"])
def test_visible_auto_follows_qt_navigation(loaded_plot, gesture):
    window = loaded_plot
    window.vb.enableAutoRange(enable=False)
    window._axis_scale_custom_auto_axes.clear()
    edit_limits(window, "x", [20, 40])
    window.open_axis_scale_dialog("y")
    controls = window._axis_scale_controls["y"]
    click(controls.visibleOnlyCheck)
    click(controls.autoRadio)
    window._axis_scale_dialog.close()
    before_x = window.vb.viewRange()[0][:]
    before_y = window.vb.viewRange()[1][:]
    if gesture == "pan":
        window.vb.setMouseMode(window.vb.PanMode)
        window.vb.set_shift_pan_axis_constraint(True)
        overlays._drag_in_view(
            window, QtCore.QPointF(30, 40), QtCore.QPointF(25, 40),
            QtCore.Qt.KeyboardModifier.ShiftModifier,
        )
    else:
        # Scrolling over the bottom axis zooms only X, retaining Y Auto.
        viewport = window.widget.viewport()
        axis = window.plot.getAxis("bottom")
        position = window.widget.mapFromScene(axis.sceneBoundingRect().center())
        event = QtGui.QWheelEvent(
            QtCore.QPointF(position), QtCore.QPointF(viewport.mapToGlobal(position)),
            QtCore.QPoint(), QtCore.QPoint(0, 120),
            QtCore.Qt.MouseButton.NoButton, QtCore.Qt.KeyboardModifier.NoModifier,
            QtCore.Qt.ScrollPhase.NoScrollPhase, False,
        )
        qtw.QApplication.sendEvent(viewport, event)
    QTest.qWait(20)
    visible = window.vb.viewRange()[0]
    assert visible != before_x
    assert window.vb.viewRange()[1] != before_y
    x, y = window.line.getData()
    selected = y[(x >= visible[0]) & (x <= visible[1])]
    assert selected.size >= 2
    assert_data_bounds(window.vb.viewRange()[1], [selected.min(), selected.max()])
    assert "y" in window._axis_scale_custom_auto_axes
    window.open_axis_scale_dialog("y")
    assert controls.autoRadio.isChecked()
    assert controls.visibleOnlyCheck.isChecked()


@pytest.mark.parametrize("loaded_plot", [True], indirect=True)
@pytest.mark.parametrize("axis, sides", [
    ("x", ("Bottom", "Left")),
    ("y", ("Bottom", "Left")),
    ("x2", ("Top", "Right")),
    ("y2", ("Top", "Right")),
])
@pytest.mark.parametrize("option", ["autoPan", "autoVisibleOnly"])
def test_filtered_options_persist_and_control_ranges(loaded_plot, axis, sides, option):
    window = loaded_plot
    window._ensure_trace_axis_viewboxes(top=True, right=True)
    owner = overlays._assign_trace_axes(window, window.label, *sides)
    other_key = next(key for key in window.lines if key != window.label)
    opposite = ("Top", "Right") if sides[0] == "Bottom" else ("Bottom", "Left")
    overlays._assign_trace_axes(window, other_key, *opposite)
    viewboxes = [window.vb, window.top_vb, window.right_vb, window.top_right_vb]
    for viewbox in viewboxes:
        viewbox.enableAutoRange(enable=False)
    window._axis_scale_custom_auto_axes.clear()
    window.lines[other_key].setData(x=[-1e6, 1e6], y=[-1e6, 1e6])
    x, y = np.array([0., 1., 2., 3.]), np.array([10., 20., 1000., 2000.])
    window.line.setData(x=x, y=y)
    owner.setRange(xRange=[0, 1.5], yRange=[0, 50], padding=0)
    qtw.QApplication.processEvents()
    window.open_axis_scale_dialog(axis)
    controls = window._axis_scale_controls[axis]
    click(controls.autoRadio)
    assert axis in window._axis_scale_custom_auto_axes
    viewbox = window._axis_scale_viewbox(axis)
    dimension = window._axis_scale_axis_number(axis)
    checkbox = controls.autoPanCheck if option == "autoPan" else controls.visibleOnlyCheck
    before_options = [vb.state[option][:] for vb in viewboxes]
    before_links = [vb.linkedView(0) for vb in viewboxes], [vb.linkedView(1) for vb in viewboxes]
    before_other_ranges = {
        name: window._axis_scale_viewbox(name).viewRange()[number][:]
        for name, number in (("x", 0), ("y", 1), ("x2", 0), ("y2", 1))
        if name != axis
    }
    span = np.ptp(viewbox.viewRange()[dimension])

    click(checkbox)
    assert checkbox.isChecked()
    for vb, previous in zip(viewboxes, before_options, strict=True):
        expected = previous[:]
        if vb is viewbox:
            expected[dimension] = True
        assert vb.state[option] == expected
    assert before_links == (
        [vb.linkedView(0) for vb in viewboxes], [vb.linkedView(1) for vb in viewboxes]
    )
    for name, expected in before_other_ranges.items():
        number = window._axis_scale_axis_number(name)
        np.testing.assert_allclose(window._axis_scale_viewbox(name).viewRange()[number], expected)
    for bounds_viewbox, _items in window._axis_scale_bound_item_groups(axis):
        assert bounds_viewbox.autoRangeEnabled()[dimension] is False

    reopen(window, axis)
    assert checkbox.isChecked()
    if option == "autoPan":
        window.line.setData(x=x + 50, y=y + 100)
        qtw.QApplication.processEvents()
        bounds = viewbox.viewRange()[dimension]
        assert np.ptp(bounds) == pytest.approx(span)
        assert np.mean(bounds) == pytest.approx(51.5 if dimension == 0 else 1105)
    else:
        # Each axis uses its trace's perpendicular visible range, including
        # the top-right owner linked to the separate X2/Y2 control viewboxes.
        bounds = viewbox.viewRange()[dimension]
        expected = [0, 1] if dimension == 0 else [10, 20]
        assert_data_bounds(bounds, expected)
        window.line.setData(x=[0, .5, 3, 4], y=[30, 40, 3000, 4000])
        qtw.QApplication.processEvents()
        assert_data_bounds(viewbox.viewRange()[dimension], [0, .5] if dimension == 0 else [30, 40])

    # A real worker refresh publishes the database data again.
    refresh_span = np.ptp(viewbox.viewRange()[dimension])
    window.refreshWindow(force=True)
    wait_for(lambda: not window.worker.running)
    window.monitor.stop()
    reopen(window, axis)
    assert checkbox.isChecked()
    assert viewbox.state[option][dimension] is True
    assert axis in window._axis_scale_custom_auto_axes
    if option == "autoPan":
        assert np.ptp(viewbox.viewRange()[dimension]) == pytest.approx(refresh_span)

    # Ordinary range changes leave the option selected, even in manual mode.
    owner.setRange(xRange=[0, 1.5], yRange=[0, 50], padding=0)
    qtw.QApplication.processEvents()
    reopen(window, axis)
    assert checkbox.isChecked()
    assert viewbox.state[option][dimension] is True
    window.line.setData(x=x, y=y)
    click(controls.autoRadio)
    click(checkbox)
    assert not checkbox.isChecked()
    assert viewbox.state[option][dimension] is False
    # Disabling either option returns to all assigned data, with normal padding.
    expected = [0, 3] if dimension == 0 else [10, 2000]
    assert_data_bounds(viewbox.viewRange()[dimension], expected)
    reopen(window, axis)
    assert not checkbox.isChecked()
    for bounds_viewbox, _items in window._axis_scale_bound_item_groups(axis):
        assert bounds_viewbox.autoRangeEnabled()[dimension] is False
