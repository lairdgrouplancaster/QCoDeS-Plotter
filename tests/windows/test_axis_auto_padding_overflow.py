"""Automatic padding rejects unsupported geometry through real Qt actions."""

import sys
from copy import deepcopy

import numpy as np
import pytest
from PyQt6 import QtCore, QtTest

from qplot.windows._plot_feedback import PlotWindowFeedbackMixin
from tests.windows.test_merged_trace_metadata import merge
from tests.windows.test_native_fft_coordinates import fft_plots as _fft_plots_fixture

fft_plots = _fft_plots_fixture

_MAXIMUM = np.finfo(float).max


@pytest.fixture(autouse=True)
def callback_errors(monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    yield
    assert errors == []


@pytest.fixture(autouse=True)
def axis_messages(monkeypatch):
    messages = []
    original = PlotWindowFeedbackMixin.show_status

    def record(window, message, *args):
        messages.append((window, message))
        original(window, message, *args)

    monkeypatch.setattr(PlotWindowFeedbackMixin, "show_status", record)
    return messages


def _controls(fft_plots, axis):
    window, (plot, source), _x, _y = fft_plots
    line = plot.line
    if axis in ("x2", "y2"):
        _key, line = merge(window, plot, source, x_axis="Top", y_axis="Right")
    plot.open_axis_scale_dialog(axis)
    return plot, line, plot._axis_scale_controls[axis], plot._axis_scale_viewbox(axis)


def _click(radio):
    QtTest.QTest.mouseClick(radio, QtCore.Qt.MouseButton.LeftButton,
                          pos=QtCore.QPoint(8, radio.height() // 2))


@pytest.mark.parametrize("axis,fft_plots", [
    (axis, {"x": np.array([0., sign * _MAXIMUM]), "y": np.array([1., 2.])})
    if axis in ("x", "x2") else
    (axis, {"x": np.array([1., 2.]), "y": np.array([0., sign * _MAXIMUM])})
    for axis in ("x", "y", "x2", "y2") for sign in (1, -1)
], indirect=["fft_plots"])
def test_initial_padding_overflow_keeps_bounded_auto_and_recovers(
    fft_plots, axis, axis_messages, qapplication,
):
    plot, line, controls, viewbox = _controls(fft_plots, axis)
    qapplication.processEvents()
    assert controls.autoRadio.isChecked()
    assert viewbox.autoRangeEnabled()[plot._axis_scale_axis_number(axis)] is False
    assert any(window is plot and "supported plot range" in message
               for window, message in axis_messages)
    before = deepcopy(viewbox.viewRange())
    assert np.all(np.isfinite(before))
    raw_x, raw_y = line.getOriginalDataset()
    expected_x, expected_y = fft_plots[2:]
    # The merged source is the second recorded QCoDeS array, in reverse order.
    np.testing.assert_array_equal(raw_x, expected_x[::-1] if axis in ("x2", "y2") else expected_x)
    np.testing.assert_array_equal(raw_y, expected_y[::-1] if axis in ("x2", "y2") else expected_y)
    _click(controls.autoRadio)
    qapplication.processEvents()
    np.testing.assert_array_equal(viewbox.viewRange(), before)
    assert controls.autoRadio.isChecked()

    # Keep the same trace and Qt data-publication signal, now with supported
    # values derived from its recorded ordinary axis. Auto must recover.
    ordinary = expected_y if axis in ("x", "x2") else expected_x
    line.setData(ordinary, ordinary + 2.)
    qapplication.processEvents()
    values = ordinary if axis in ("x", "x2") else ordinary + 2.
    lower, upper = viewbox.viewRange()[plot._axis_scale_axis_number(axis)]
    assert lower <= values.min() < values.max() <= upper
    assert controls.autoRadio.isChecked()


@pytest.mark.parametrize("axis,fft_plots", [
    (axis, {"x": np.array([0., sign * _MAXIMUM]), "y": np.array([1., 2.])})
    if axis in ("x", "x2") else
    (axis, {"x": np.array([1., 2.]), "y": np.array([0., sign * _MAXIMUM])})
    for axis in ("x", "y", "x2", "y2") for sign in (1, -1)
], indirect=["fft_plots"])
def test_rejected_auto_click_restores_manual_controls_and_view(
    fft_plots, axis, axis_messages, qapplication,
):
    plot, line, controls, viewbox = _controls(fft_plots, axis)
    setter = viewbox.setXRange if axis in ("x", "x2") else viewbox.setYRange
    setter(0.1, 0.9, padding=0)
    qapplication.processEvents()
    assert controls.manualRadio.isChecked()
    before = deepcopy(viewbox.getState())
    fields = controls.minText.text(), controls.maxText.text()
    original = tuple(values.copy() for values in line.getOriginalDataset())
    axis_messages.clear()
    _click(controls.autoRadio)
    qapplication.processEvents()
    assert controls.manualRadio.isChecked()
    assert viewbox.getState()["targetRange"] == before["targetRange"]
    assert viewbox.getState()["autoRange"] == before["autoRange"]
    assert (controls.minText.text(), controls.maxText.text()) == fields
    assert any(window is plot and "supported plot range" in message
               for window, message in axis_messages)
    for values, recorded in zip(line.getOriginalDataset(), original, strict=True):
        np.testing.assert_array_equal(values, recorded)
