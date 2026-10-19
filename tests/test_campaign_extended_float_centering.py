"""Mean subtraction retains cancellation residuals recorded by QCoDeS."""

from fractions import Fraction

import numpy as np
import pytest

from qplot.tools.plot_tools import _center_float_samples
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


def _signal(dtype, sign):
    return sign * np.array([dtype(1) + np.finfo(dtype).eps, -1, 0], dtype=dtype)


@pytest.mark.parametrize("measured_plots", [
    (np.arange(3.), _signal(dtype, sign), 1)
    for dtype in (np.float16, np.float32, np.float64, np.longdouble)
    for sign in (-1, 1)
] + [
    (np.arange(3.), np.tile(_signal(dtype, sign), (2, 1)), 1)
    for dtype in (np.float16, np.float32, np.float64, np.longdouble)
    for sign in (-1, 1)
] + [
    (np.arange(2.), np.tile(_signal(dtype, sign)[:, None], (1, 2)), 1)
    for dtype in (np.float16, np.float32, np.float64, np.longdouble)
    for sign in (-1, 1)
], indirect=True)
def test_actual_qcodes_cancellation_centering(measured_plots, no_callback_errors):
    _window, (host,), _x, source = measured_plots
    samples = source if source.ndim == 1 else source[0] if source.shape[1] == 3 else source[:, 0]
    exact = [Fraction(*value.as_integer_ratio()) for value in samples]
    mean = sum(exact) / len(exact)
    expected = np.array([float(value - mean) for value in exact])
    assert expected[2] != 0

    if source.ndim == 1:
        host.plot.ctrl.subtractMeanCheck.setChecked(True)
        actual = host.line.getData()[1]
        np.testing.assert_array_equal(host.line.getOriginalDataset()[1], source)
        assert host.line._qplot_precision_error is None
    elif source.shape[1] == 3:
        operation_option(host, "Subtract Row Mean").input.setChecked(True)
        assert apply_operations(host)[1:] == ([True], [])
        actual = host.dataGrid[0]
    else:
        operation_option(host, "Subtract Column Mean").input.setChecked(True)
        assert apply_operations(host)[1:] == ([True], [])
        actual = host.dataGrid[:, 0]
    np.testing.assert_array_equal(np.asarray(actual, dtype=float), expected)


@pytest.mark.parametrize("dtype", [np.float64, np.longdouble])
def test_centered_extremes_retain_dtype_range(dtype):
    bound = np.finfo(dtype).max
    source = np.array([bound, -bound, 0], dtype=dtype)
    np.testing.assert_array_equal(_center_float_samples(source), source)


@pytest.mark.parametrize("dtype", [np.float64, np.longdouble])
def test_centered_subnormal_residuals_round_after_subtraction(dtype):
    tiny = np.nextafter(dtype(0), dtype(1))
    source = np.array([tiny, tiny, 0], dtype=dtype)
    np.testing.assert_array_equal(_center_float_samples(source), [dtype(0), dtype(0), -tiny])


@pytest.mark.parametrize("exponent", [-1000, -3, 0, 17, 1000])
def test_extended_centering_rounds_halfway_residuals_to_even(exponent):
    if np.finfo(np.longdouble).nmant <= np.finfo(float).nmant:
        pytest.skip("Platform has no extended floating dtype")
    start = np.ldexp(np.longdouble(1), exponent)
    step = np.spacing(start)
    for index in range(30):
        left = start + index * step
        right = np.nextafter(left, np.longdouble(np.inf))
        # Subtracting their opposite-sign mean gives the exact midpoint.
        # Adjacent significands alternate between even and odd low bits.
        expected = right if index % 2 else left
        actual = _center_float_samples(np.array([left, -right], dtype=np.longdouble))
        np.testing.assert_array_equal(actual, [expected, -expected])


@pytest.mark.parametrize("cancel_at", [3, 20])
def test_extended_exact_centering_is_cancellable(cancel_at):
    if np.finfo(np.longdouble).nmant <= np.finfo(float).nmant:
        pytest.skip("Platform has no extended floating dtype")
    source = np.tile(_signal(np.longdouble, 1), 5000)
    original = source.copy()
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= cancel_at

    with pytest.raises(InterruptedError, match="cancelled"):
        _center_float_samples(source, cancelled_callback=cancelled)
    assert calls == cancel_at
    np.testing.assert_array_equal(source, original)
