"""Current QCoDeS floating arrays retain their variation in display and stats."""

from decimal import Decimal, localcontext
from fractions import Fraction

import numpy as np
import pytest
from PyQt6 import QtCore

from qplot.tools.sample_statistics import finite_mean, finite_standard_deviation
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors,
)
from tests.windows.test_numerical_stability import measured_plots as _measured_plots

measured_plots = _measured_plots
no_callback_errors = _no_callback_errors


def _exact_summary(values):
    samples = [Fraction(*value.as_integer_ratio()) for value in values.flat]
    mean = sum(samples) / len(samples)
    variance = sum((sample - mean) ** 2 for sample in samples) / len(samples)
    with localcontext() as context:
        context.prec = 90
        deviation = (Decimal(variance.numerator) / Decimal(variance.denominator)).sqrt()
    return float(mean), float(deviation)


@pytest.mark.parametrize("dtype", [np.float64, np.longdouble])
@pytest.mark.parametrize("sign", [-1, 1])
def test_extended_mean_outside_display_range_remains_a_numeric_result(dtype, sign):
    value = dtype(sign) * np.finfo(dtype).max
    samples = np.full(3, value, dtype=dtype)
    assert finite_mean(samples) == float(value)
    assert finite_standard_deviation(samples) == 0.


@pytest.mark.parametrize("measured_plots", [
    (np.arange(3.), np.array([np.finfo(np.longdouble).max, 1.,
                              np.finfo(np.longdouble).max], dtype=np.longdouble), 1),
], indirect=True)
def test_recorded_extended_mean_overflow_does_not_escape_fft_control(
    measured_plots, no_callback_errors,
):
    _window, (host,), _x, signal = measured_plots
    host.plot.ctrl.fftCheck.setChecked(True)
    assert host.plot.ctrl.fftCheck.isChecked()
    np.testing.assert_array_equal(host.line.getOriginalDataset()[1], signal)


@pytest.mark.parametrize("measured_plots", [
    (np.arange(3.), dtype(1) + np.arange(3, dtype=dtype) * np.finfo(dtype).eps, 1)
    for dtype in (np.float32, np.float64, np.longdouble)
], indirect=True)
def test_recorded_float_display_guard_and_centering_recovery(measured_plots):
    _window, (host,), _x, signal = measured_plots
    extended = np.finfo(signal.dtype).nmant > np.finfo(float).nmant
    np.testing.assert_array_equal(host.line.getOriginalDataset()[1], signal)
    assert np.unique(signal).size == 3
    if extended:
        assert host.line._qplot_precision_error
        assert host.line.getData()[0].size == 0
        host.set_marquee_rect(QtCore.QRectF(-.5, .9, 3., .2))
        assert host._marquee_stats_text() is None
    else:
        assert host.line._qplot_precision_error is None
        path = host.line.curve.getPath()
        assert len({path.elementAt(index).y for index in range(path.elementCount())}) == 3

    # Use the existing native control to recover a representable relative
    # signal while keeping recorded absolute samples available for export.
    host.plot.ctrl.subtractMeanCheck.setChecked(True)
    assert host.line._qplot_precision_error is None
    epsilon = float(np.finfo(signal.dtype).eps)
    np.testing.assert_array_equal(host.line.getData()[1], [-epsilon, 0., epsilon])
    np.testing.assert_array_equal(host.line.getOriginalDataset()[1], signal)
    host.set_marquee_rect(QtCore.QRectF(-.5, -2 * epsilon, 3., 4 * epsilon))
    mean, deviation = _exact_summary(host.line.getData()[1])
    text = host._marquee_stats_text()
    assert f"Average: {host.formatNum(mean)}" in text
    assert f"Standard deviation: {host.formatNum(deviation)}" in text


_EPS64 = np.longdouble(np.finfo(float).eps)
_VISIBLE_NEARBY = np.array([
    1 + _EPS64 * np.longdouble(".4"),
    1 + _EPS64 * np.longdouble(".6"),
], dtype=np.longdouble)
_CANCELLING = np.array([
    np.nextafter(np.longdouble(1), np.longdouble(2)), -1,
], dtype=np.longdouble)


@pytest.mark.parametrize("measured_plots", [
    (np.arange(2.), signal, 1) for signal in (_VISIBLE_NEARBY, _CANCELLING)
], indirect=True)
def test_visible_recorded_float_statistics_keep_original_precision(measured_plots):
    _window, (host,), _x, signal = measured_plots
    # These samples remain distinct at Qt's display precision, so statistics
    # must still use their stored precision even after the guard is repaired.
    assert np.unique(signal.astype(float)).size == 2
    assert host.line._qplot_precision_error is None
    host.set_marquee_rect(QtCore.QRectF(-.5, -2., 2., 4.))
    mean, deviation = _exact_summary(signal)
    text = host._marquee_stats_text()
    assert f"Average: {host.formatNum(mean)}" in text
    assert f"Standard deviation: {host.formatNum(deviation)}" in text
    menu = host._new_marquee_context_menu()
    next(action for action in menu.actions() if action.text() == "Stats...").trigger()
    assert host._marquee_stats_dialog.isVisible()
    host._marquee_stats_dialog.close()


@pytest.mark.parametrize("measured_plots", [
    (np.arange(3.), np.tile(np.longdouble(1) + np.arange(3, dtype=np.longdouble)
                            * np.finfo(np.longdouble).eps, (2, 1)), 1),
], indirect=True)
def test_recorded_extended_heatmap_guard_and_row_mean_recovery(measured_plots):
    _window, (host,), _x, grid = measured_plots
    if np.finfo(grid.dtype).nmant > np.finfo(float).nmant:
        assert host._heatmap_color_range_rejected
    np.testing.assert_array_equal(host.dataGrid, grid)
    operation_option(host, "Subtract Row Mean").input.setChecked(True)
    assert apply_operations(host)[1:] == ([True], [])
    epsilon = float(np.finfo(grid.dtype).eps)
    np.testing.assert_array_equal(host.dataGrid, np.tile([-epsilon, 0., epsilon], (2, 1)))
    assert not host._heatmap_color_range_rejected
    host.set_marquee_rect(QtCore.QRectF(-.5, -.5, 3., 2.))
    _mean, deviation = _exact_summary(host.dataGrid)
    assert f"Standard deviation: {host.formatNum(deviation)}" in host._marquee_stats_text()
