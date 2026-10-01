"""Exercise autoscale options in loaded qPlot line windows, with linked axes."""

import numpy as np
import pytest
from PyQt6 import QtCore
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
