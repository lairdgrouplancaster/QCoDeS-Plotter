"""Derivatives retain recorded extended-precision axis coordinates."""

from fractions import Fraction

import numpy as np
import pytest

from qplot.tools.plot_tools import differentiate
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors,
)
from tests.windows.test_numerical_stability import measured_plots as _measured_plots
from tests.windows.test_plot_integration import wait_for

measured_plots = _measured_plots
no_callback_errors = _no_callback_errors

_EXTENDED = np.finfo(np.longdouble).nmant > np.finfo(float).nmant
_X = 1 + np.array([".4", ".6"], dtype=np.longdouble) * np.longdouble(np.finfo(float).eps)
_HEATMAP_X = np.array([0., 1., 3.], dtype=np.longdouble)


@pytest.mark.skipif(not _EXTENDED, reason="Platform has no extended floating precision")
@pytest.mark.parametrize("measured_plots", [
    (_X, _X.copy(), 1), (_HEATMAP_X, np.tile(_HEATMAP_X, (2, 1)), 1),
], indirect=True)
@pytest.mark.parametrize("swapped", [False, True])
def test_recorded_extended_coordinate_derivative_controls(
    measured_plots, no_callback_errors, swapped,
):
    _window, (host,), x, y = measured_plots
    heatmap = y.ndim == 2
    if not heatmap:
        assert host.line._qplot_precision_error is None
    if swapped:
        assert host.set_plot_axes_swapped(True)
        wait_for(lambda: not host.worker.running)
        host.monitor.stop()
    name = "dz/dy" if heatmap and swapped else "dz/dx" if heatmap else "dy/dx"
    operation_option(host, name).input.setChecked(True)
    assert apply_operations(host)[1:] == ([True], [])
    actual = host.dataGrid if heatmap else host.axis_data["y"]
    np.testing.assert_allclose(actual, np.ones_like(actual), rtol=1e-14)


def _fraction(value):
    return (Fraction(int(value)) if isinstance(value, (int, np.integer))
            else Fraction(*value.as_integer_ratio()))


def _gradient_oracle(x, y):
    x, y = [[_fraction(value) for value in values] for values in (x, y)]
    slopes = [(y[index + 1] - y[index]) / (x[index + 1] - x[index])
              for index in range(len(x) - 1)]
    inner = [((x[index + 1] - x[index]) * slopes[index - 1]
              + (x[index] - x[index - 1]) * slopes[index]) / (x[index + 1] - x[index - 1])
             for index in range(1, len(x) - 1)]
    return np.array([float(value) for value in [slopes[0], *inner, slopes[-1]]])


@pytest.mark.skipif(not _EXTENDED, reason="Platform has no extended floating precision")
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("signal_dtype", [float, np.longdouble, np.int64, object])
@pytest.mark.parametrize("steps", [[0, 1, 2], [0, 1, 3, 7]])
def test_extended_coordinate_stencils_match_exact_recorded_values(reverse, signal_dtype, steps):
    x = 1 + np.array(steps, dtype=np.longdouble) * np.finfo(np.longdouble).eps
    y = np.array([0, 1, -2, 4][:len(x)], dtype=signal_dtype)
    if reverse:
        x, y = x[::-1], y[::-1]
    actual = differentiate("x", dict(x=x, y=y, z=None))["y"]
    np.testing.assert_allclose(actual, _gradient_oracle(x, y), rtol=1e-14, atol=0)


@pytest.mark.parametrize("axis", ["x", "y"])
def test_extended_coordinate_grid_stencils_keep_axes_and_missingness(axis):
    x = 1 + np.arange(3, dtype=np.longdouble) * np.finfo(np.longdouble).eps
    samples = np.array([[1., np.nan, 3.], [2., 4., 6.]])
    grid = samples if axis == "x" else samples.T
    axes = dict(x=x, y=np.arange(2.)) if axis == "x" else dict(x=np.arange(2.), y=x)
    actual = differentiate(axis, dict(**axes, z=grid))["z"]
    actual = actual if axis == "x" else actual.T
    expected = 1 / float(np.finfo(np.longdouble).eps)
    np.testing.assert_array_equal(actual[0], [np.nan, expected, np.nan])
    np.testing.assert_array_equal(actual[1], [2 * expected] * 3)
    np.testing.assert_array_equal(grid, samples if axis == "x" else samples.T)


@pytest.mark.parametrize("exponent,amplitude", [
    (-1070, 1e-308), (-1000, 1.), (-400, 1.), (0, 1.),
    (400, 1.), (1000, 1.), (1030, 1e300),
])
def test_extended_coordinates_with_float64_signal_keep_representable_slopes(exponent, amplitude):
    if not _EXTENDED and abs(exponent) > 1000:
        pytest.skip("Coordinate exponent needs an extended floating dtype")
    x = np.ldexp(np.array([0., 1., 3.], dtype=np.longdouble), exponent)
    y = np.array([0., 1., 3.]) * amplitude
    actual = differentiate("x", dict(x=x, y=y, z=None))["y"]
    assert actual.dtype == np.dtype(float)
    np.testing.assert_allclose(actual, _gradient_oracle(x, y), rtol=1e-14, atol=0)


@pytest.mark.parametrize("x", [[0., 1., 1.], [0., 2., 1.], [0., np.inf, 2.]])
def test_extended_coordinates_retain_invalid_axis_diagnostics(x):
    with pytest.raises(ValueError, match="coordinates"):
        differentiate("x", dict(x=np.asarray(x, dtype=np.longdouble), y=np.arange(3.), z=None))
