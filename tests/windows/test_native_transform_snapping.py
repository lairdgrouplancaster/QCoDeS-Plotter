"""Native transform snapping through real QCoDeS windows and Qt signals."""

import sys
import traceback

import numpy as np
import pytest
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.windows._plot1d_snap import _line_snap_data
from tests.windows.test_native_transform_labels import (
    SUPPORTED_CONTROLS,
    click_control,
    set_controls,
)
from tests.windows.test_plot_overlay_integration import _assign_trace_axes
from tests.windows.test_plot_overlay_integration import (
    loaded_plot as _loaded_plot_fixture,
)

loaded_plot = _loaded_plot_fixture


@pytest.fixture
def no_callback_errors(monkeypatch, qapplication):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    yield
    qapplication.processEvents()
    assert not errors, ["".join(traceback.format_exception(*error)) for error in errors]


def assert_snap(window, owner, key, physical_x, physical_y, view_x, view_y, index):
    point = QtCore.QPointF(float(view_x[index]), float(view_y[index]))
    scene_pos = owner.mapViewToScene(point)
    assert window.plot.sceneBoundingRect().contains(scene_pos)
    window.plot.scene().sigMouseMoved.emit(scene_pos)
    assert window.pos_labels["x"].text() == f"x = {window.formatNum(physical_x[index])};"
    assert window.pos_labels["y"].text() == f"y = {window.formatNum(physical_y[index])}"
    assert window.pos_labels["index"].text() == f"[{index}]"
    assert f"snapped to point {index})" in window.trace_label.text()
    nearest = window._nearest_trace_point(scene_pos)
    assert nearest.label == key
    assert nearest.viewbox is owner
    assert nearest.point_number == index + 1
    assert nearest.x_value == pytest.approx(physical_x[index])
    assert nearest.y_value == pytest.approx(physical_y[index])
    assert nearest.x_view_value == pytest.approx(view_x[index])
    assert nearest.y_view_value == pytest.approx(view_y[index])
    assert window._snap_marker_view is owner
    assert window.snap_marker.parentItem() is owner.childGroup
    np.testing.assert_allclose(window.snap_marker.getData(), [[view_x[index]], [view_y[index]]])


def test_phase_fft_log_x_keeps_first_sample_and_cursor_values(loaded_plot, no_callback_errors):
    window = loaded_plot
    np.testing.assert_array_equal(window.line.getOriginalDataset(), [np.arange(64), np.arange(10, 74)])
    click_control(window, "phasemapCheck")
    click_control(window, "fftCheck")
    click_control(window, "logXCheck")
    window.snap_to_trace_action.trigger()
    assert window.snap_to_trace_action.isChecked()
    physical_x, physical_y = np.arange(10, 73), np.ones(63)
    view_x, view_y = np.log10(physical_x), physical_y
    window.vb.setRange(xRange=[0.9, 1.9], yRange=[0.5, 1.5], padding=0)
    qtw.QApplication.processEvents()
    np.testing.assert_allclose(window.line.getData(), [view_x, view_y])
    for index in (0, 62):
        assert_snap(window, window.vb, window.label, physical_x, physical_y, view_x, view_y, index)
    # Also exercise the viewport mouse event that feeds the scene signal.
    point = window.vb.mapViewToScene(QtCore.QPointF(1, 1))
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
    assert window.pos_labels["x"].text() == f"x = {window.formatNum(10)};"
    assert window.pos_labels["index"].text() == "[0]"
    # Log toggles and leaving/re-entering phase map must rebuild the sample
    # mapping without shifting indices or retaining the previous marker owner.
    for log_x in (False, True):
        click_control(window, "logXCheck")
        view_x = logarithmic(physical_x, log_x)
        set_sample_range(window.vb, view_x, view_y, 0, 62)
        for index in (0, 62):
            assert_snap(window, window.vb, window.label, physical_x, physical_y, view_x, view_y, index)
    for phase in (False, True):
        click_control(window, "phasemapCheck")
        controls = ("phasemapCheck", "fftCheck") if phase else ("fftCheck",)
        physical_x, physical_y = processed_samples(64, controls)
        view_x, view_y = np.log10(physical_x), physical_y
        first, last = 0, len(view_x) - 1
        set_sample_range(window.vb, view_x, view_y, first, last)
        for index in (first, last):
            assert_snap(window, window.vb, window.label, physical_x, physical_y, view_x, view_y, index)


def processed_samples(count, controls, log_x=True):
    """Independent numerical oracle for these unit-spaced, unit-slope runs."""
    x, y = np.arange(count, dtype=float), np.arange(10, count + 10, dtype=float)
    if "phasemapCheck" in controls:
        return y[:-1], np.ones(count - 1)
    if "derivativeCheck" in controls:
        return x[:-1], np.ones(count - 1)
    if "subtractMeanCheck" in controls:
        y = y - np.mean(y)
    if "fftCheck" in controls:
        x, y = np.fft.rfftfreq(count), np.abs(np.fft.rfft(y) / count)
        if log_x:
            x, y = x[1:], y[1:]
    return x, y


def logarithmic(values, enabled):
    if not enabled:
        return values
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.log10(values)
    result[~np.isfinite(result)] = np.nan
    return result


def assert_full_snap_data(line, physical_x, physical_y, view_x, view_y):
    data = _line_snap_data(line)
    assert data is not None
    for actual, expected in zip((data.x_raw, data.y_raw, data.x_view, data.y_view),
                                (physical_x, physical_y, view_x, view_y), strict=True):
        assert len(actual) == len(physical_x)
        np.testing.assert_allclose(actual, expected, equal_nan=True, atol=1e-14)


def set_sample_range(owner, view_x, view_y, start, stop):
    x_span = view_x[stop] - view_x[start]
    y_low, y_high = np.nanmin(view_y), np.nanmax(view_y)
    y_margin = max((y_high - y_low) * 0.05, 0.1)
    owner.setRange(xRange=[view_x[start] - x_span * 0.01, view_x[stop] + x_span * 0.01],
                   yRange=[y_low - y_margin, y_high + y_margin], padding=0)
    qtw.QApplication.processEvents()


@pytest.mark.parametrize("loaded_plot", [True], indirect=True)
@pytest.mark.parametrize("controls", SUPPORTED_CONTROLS)
@pytest.mark.parametrize("subtract_mean", [False, True])
def test_supported_native_snap_samples_and_secondary_ownership(
    loaded_plot, no_callback_errors, controls, subtract_mean,
):
    window = loaded_plot
    if subtract_mean:
        controls = (*controls, "subtractMeanCheck")
    set_controls(window, controls)
    window.snap_to_trace_action.trigger()
    for key, line in window.lines.items():
        for other in window.lines.values():
            other.setVisible(other is line)
        count = len(line.getOriginalDataset()[0])
        physical_x, physical_y = processed_samples(count, controls)
        view_x = logarithmic(physical_x, True)
        for axes in (("Bottom", "Left"), ("Top", "Right")):
            owner = _assign_trace_axes(window, key, *axes)
            x_axis, y_axis = ("x", "y") if axes[0] == "Bottom" else ("x2", "y2")
            window._axis_scale_log_toggled(x_axis, True)
            for log_y in (False, True):
                window._axis_scale_log_toggled(y_axis, log_y)
                view_y = logarithmic(physical_y, log_y)
                finite = np.flatnonzero(np.isfinite(view_x) & np.isfinite(view_y))
                first, last = finite[0], finite[-1]
                set_sample_range(owner, view_x, view_y, first, last)
                np.testing.assert_allclose(line.getData(), [view_x, view_y], equal_nan=True, atol=1e-14)
                assert_full_snap_data(line, physical_x, physical_y, view_x, view_y)
                for index in (first, (first + last) // 2, last):
                    assert_snap(window, owner, key, physical_x, physical_y, view_x, view_y, index)


@pytest.mark.parametrize("loaded_plot", [True], indirect=True)
@pytest.mark.parametrize("controls", [
    ("fftCheck",), ("phasemapCheck", "fftCheck"),
    ("phasemapCheck", "fftCheck", "derivativeCheck", "subtractMeanCheck"),
])
@pytest.mark.parametrize("method", ["subsample", "mean", "peak"])
@pytest.mark.parametrize("axes", [("Bottom", "Left"), ("Bottom", "Right"),
                                 ("Top", "Left"), ("Top", "Right")])
def test_native_snap_uses_full_samples_with_clipping_and_downsampling(
    loaded_plot, no_callback_errors, controls, method, axes,
):
    window = loaded_plot
    set_controls(window, controls)
    window.snap_to_trace_action.trigger()
    # Establish ownership before activating rendering reductions. Each case
    # then snaps both primary and merged data in their assigned viewbox.
    for key in window.lines:
        _assign_trace_axes(window, key, *axes)
    window._axis_scale_log_toggled("x2" if axes[0] == "Top" else "x", True)
    window._axis_scale_log_toggled("y2" if axes[1] == "Right" else "y", False)
    window.plot.setDownsampling(ds=8, auto=False, mode=method)
    window.plot.ctrl.clipToViewCheck.setChecked(True)
    for key, line in window.lines.items():
        for other in window.lines.values():
            other.setVisible(other is line)
        count = len(line.getOriginalDataset()[0])
        physical_x, physical_y = processed_samples(count, controls)
        view_x, view_y = np.log10(physical_x), physical_y
        owner = line.getViewBox()
        assert owner is window._trace_axis_viewbox(window._trace_styles[key])
        full_display_x = None
        for start, stop in ((0, len(view_x) - 1), (9, 18)):
            set_sample_range(owner, view_x, view_y, start, stop)
            display_x, display_y = line.getData()
            assert len(display_x) == len(display_y) < len(view_x)
            assert line.opts["clipToView"]
            if full_display_x is None:
                full_display_x = display_x.copy()
            else:
                assert not np.array_equal(display_x, full_display_x)
            assert_full_snap_data(line, physical_x, physical_y, view_x, view_y)
            omitted = [index for index in range(start, stop + 1)
                       if not np.any(np.isclose(display_x, view_x[index], atol=1e-14)
                                     & np.isclose(display_y, view_y[index], atol=1e-14))]
            assert omitted  # The snap oracle must include samples absent from rendering.
            for index in (start, omitted[len(omitted) // 2], stop):
                assert_snap(window, owner, key, physical_x, physical_y, view_x, view_y, index)
