"""Free cursor follows the measured main trace through axis reassignment."""

import numpy as np
import pytest
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_plot_overlay_integration import loaded_plot as loaded_plot


def assign_axes(window, x_side, y_side):
    window.open_trace_appearance_dialog(window.label)
    dialog = window._trace_appearance_dialog
    dialog.x_axis.setCurrentText(x_side)
    dialog.y_axis.setCurrentText(y_side)
    dialog.reject()
    qtw.QApplication.processEvents()
    return window.line.getViewBox()


def configure_ranges(window, x, y):
    # Separate unused axes by orders of magnitude; linked owners retain the
    # assigned main trace's actual ranges below.
    window.vb.setRange(xRange=[1000, 3000], yRange=[10000, 30000], padding=0)
    if window.top_vb is not None:
        window.top_vb.setXRange(1000, 3000, padding=0)
    if window.right_vb is not None:
        window.right_vb.setYRange(10000, 30000, padding=0)
    for dimension, values in [("x", x), ("y", y)]:
        axis = window._axis_scale_axis_for_line(window.line, dimension)
        finite = np.asarray(values)[np.isfinite(values)]
        low, high = np.min(finite), np.max(finite)
        margin = max((high - low) * 0.05, 0.2)
        owner = window._axis_scale_viewbox(axis)
        setter = owner.setXRange if dimension == "x" else owner.setYRange
        setter(float(low - margin), float(high + margin), padding=0)
    qtw.QApplication.processEvents()


def assert_scene_cursor(window, physical_x, physical_y, view_x, view_y, index):
    owner = window.line.getViewBox()
    point = owner.mapViewToScene(
        QtCore.QPointF(float(view_x[index]), float(view_y[index]))
    )
    assert window.plot.sceneBoundingRect().contains(point)
    window.plot.scene().sigMouseMoved.emit(point)
    assert float(
        window.pos_labels["x"].text().removeprefix("x = ").removesuffix(";")
    ) == pytest.approx(physical_x[index], rel=0.006)
    assert float(window.pos_labels["y"].text().removeprefix("y = ")) == pytest.approx(
        physical_y[index], rel=0.006
    )
    assert window.pos_labels["index"].text() == f"[{index}]"
    return point


@pytest.mark.parametrize(
    "axes", [("Bottom", "Left"), ("Bottom", "Right"), ("Top", "Left"), ("Top", "Right")]
)
@pytest.mark.parametrize(
    "log_modes", [(False, False), (True, False), (False, True), (True, True)]
)
def test_free_cursor_main_axis_pair_and_log_inverse(loaded_plot, axes, log_modes):
    window = loaded_plot
    owner = assign_axes(window, *axes)
    window.open_axis_scale_dialog("x")
    for dimension, enabled in zip(("x", "y"), log_modes, strict=True):
        axis = window._axis_scale_axis_for_line(window.line, dimension)
        window._axis_scale_controls[axis].logCheck.setChecked(enabled)
    physical_x = np.arange(64, dtype=float)
    physical_y = np.arange(10, 74, dtype=float)
    with np.errstate(divide="ignore"):
        view_x = np.log10(physical_x) if log_modes[0] else physical_x
        view_y = np.log10(physical_y) if log_modes[1] else physical_y
    window.plot.setDownsampling(ds=8, auto=False, mode="peak")
    configure_ranges(window, view_x, view_y)
    assert not window.snap_to_trace_action.isChecked()
    for index in (11, 32, 51):
        assert_scene_cursor(window, physical_x, physical_y, view_x, view_y, index)
    # Actual viewport movement also reaches the scene's connected Qt slot.
    point = owner.mapViewToScene(QtCore.QPointF(float(view_x[32]), float(view_y[32])))
    pixel = window.widget.mapFromScene(point)
    viewport = window.widget.viewport()
    event = QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseMove,
        QtCore.QPointF(pixel),
        QtCore.QPointF(viewport.mapToGlobal(pixel)),
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    qtw.QApplication.sendEvent(viewport, event)
    qtw.QApplication.processEvents()
    assert window.pos_labels["index"].text() == "[32]"


@pytest.mark.parametrize("loaded_plot", [True], indirect=True)
@pytest.mark.parametrize("control", ["derivativeCheck", "phasemapCheck", "fftCheck"])
def test_free_cursor_transformed_main_trace_survives_merged_axis_reassignment(
    loaded_plot, control
):
    window = loaded_plot
    assert len(window.lines) == 2
    click_control(window, control)
    original_x = np.arange(64, dtype=float)
    original_y = np.arange(10, 74, dtype=float)
    if control == "derivativeCheck":
        x, y = original_x[:-1], np.ones(63)
    elif control == "phasemapCheck":
        x, y = original_y[:-1], np.ones(63)
    else:
        x, y = np.fft.rfftfreq(64), np.abs(np.fft.rfft(original_y) / 64)
    window.plot.setDownsampling(ds=4, auto=False, mode="peak")
    for axes in [
        ("Top", "Right"),
        ("Bottom", "Right"),
        ("Top", "Left"),
        ("Bottom", "Left"),
    ]:
        assign_axes(window, *axes)
        configure_ranges(window, x, y)
        for index in (5, len(x) // 2, len(x) - 6):
            assert_scene_cursor(window, x, y, x, y, index)


@pytest.mark.parametrize("line_state", ["missing", "detached"])
def test_free_cursor_uses_primary_axes_without_owned_main_line(loaded_plot, line_state):
    window = loaded_plot
    assign_axes(window, "Top", "Right")
    window.open_axis_scale_dialog("x2")
    window._axis_scale_controls["x2"].logCheck.setChecked(True)
    line = window.line
    try:
        if line_state == "missing":
            window.line = None
        else:
            line.getViewBox().removeItem(line)
        window.vb.setRange(xRange=[10, 30], yRange=[100, 300], padding=0)
        qtw.QApplication.processEvents()
        point = window.vb.mapViewToScene(QtCore.QPointF(20, 200))
        window.plot.scene().sigMouseMoved.emit(point)
        assert (
            float(window.pos_labels["x"].text().removeprefix("x = ").removesuffix(";"))
            == 20
        )
        assert float(window.pos_labels["y"].text().removeprefix("y = ")) == 200
    finally:
        window.line = line
        window._apply_trace_style(window.label, line)
