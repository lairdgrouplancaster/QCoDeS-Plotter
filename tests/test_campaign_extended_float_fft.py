"""Extended QCoDeS arrays support native nonuniform FFT interpolation."""

from fractions import Fraction

import numpy as np
import pytest

from qplot.windows._native_transforms import NativePlotDataItem
from tests.windows.test_native_transform_labels import (
    click_control,
)
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors,
)
from tests.windows.test_numerical_stability import measured_plots as _measured_plots
from tests.windows.test_plot_integration import wait_for

measured_plots = _measured_plots
no_callback_errors = _no_callback_errors


def _exact_resampling(x, y):
    """Interpolate on ideal uniform positions before rounding the samples."""
    coordinates = [Fraction(*value.as_integer_ratio()) for value in x]
    values = [Fraction(*value.as_integer_ratio()) for value in y]
    if coordinates[0] > coordinates[-1]:
        coordinates.reverse()
        values.reverse()
    targets = [coordinates[0] + (coordinates[-1] - coordinates[0]) * index / (len(x) - 1)
               for index in range(len(x))]
    resampled = []
    for target in targets:
        for index in range(len(x) - 1):
            if coordinates[index] <= target <= coordinates[index + 1]:
                weight = (target - coordinates[index]) / (coordinates[index + 1] - coordinates[index])
                resampled.append(values[index] + weight * (values[index + 1] - values[index]))
                break
    return resampled


@pytest.mark.parametrize("measured_plots", [
    (np.array([0., 1., 3.], dtype=x_dtype), np.array([1., 2., 4.], dtype=y_dtype), 1)
    for x_dtype, y_dtype in ((np.longdouble, float), (float, np.longdouble),
                             (np.longdouble, np.longdouble))
], indirect=True)
@pytest.mark.parametrize("subtract_mean", [False, True])
def test_recorded_extended_nonuniform_fft_controls_refresh_and_recovery(
    measured_plots, no_callback_errors, subtract_mean,
):
    _window, (host,), x, y = measured_plots
    try:
        if subtract_mean:
            click_control(host, "subtractMeanCheck")
        click_control(host, "fftCheck")
        expected = np.array([1., 2.5, 4.])
        if subtract_mean:
            expected -= float(sum(Fraction(float(value)) for value in y) / len(y))
        magnitudes = np.abs(np.fft.rfft(expected) / len(expected))
        for refresh in (False, True):
            if refresh:
                host.refreshWindow(force=True)
                wait_for(lambda: not host.worker.running)
                host.monitor.stop()
            actual_x, actual_y = host.line.getData()
            np.testing.assert_allclose(actual_x, [0., 2. / 9.], rtol=1e-15)
            np.testing.assert_allclose(actual_y, magnitudes, rtol=1e-14, atol=1e-17)
            raw_x, raw_y = host.line.getOriginalDataset()
            assert raw_x.dtype == x.dtype and raw_y.dtype == y.dtype
            np.testing.assert_array_equal(raw_x, x)
            np.testing.assert_array_equal(raw_y, y)
    finally:
        # A broken callback must not keep failing during fixture teardown.
        host.plot.ctrl.fftCheck.setChecked(False)
        host.plot.ctrl.subtractMeanCheck.setChecked(False)
    np.testing.assert_array_equal(host.line.getData()[1], y)


@pytest.mark.skipif(np.finfo(np.longdouble).nmant <= np.finfo(float).nmant,
                    reason="Platform has no extended floating precision")
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("case", ["coordinate_offset", "signal_offset", "cancelling_signal"])
def test_extended_interpolation_preserves_offset_and_cancellation_residuals(case, reverse):
    epsilon = np.finfo(np.longdouble).eps
    x = np.array([0., 1., 3.], dtype=np.longdouble)
    y = np.array([1., 2., 4.], dtype=np.longdouble)
    if case == "coordinate_offset":
        x = 1 + x * epsilon
    elif case == "signal_offset":
        y = 1 + x * epsilon
    else:
        y = np.array([1 + epsilon, -3., 1.], dtype=np.longdouble)
    if reverse:
        x, y = x[::-1], y[::-1]
    samples = _exact_resampling(x, y)
    item = NativePlotDataItem(x, y)
    item.setFftMode(True)
    actual_x, actual_y = item.getData()
    spacing = abs(Fraction(*x[-1].as_integer_ratio()) - Fraction(*x[0].as_integer_ratio())) / 2
    np.testing.assert_allclose(actual_x, [0., float(Fraction(1, 3) / spacing)], rtol=1e-15)
    assert actual_y[0] == abs(float(sum(samples) / len(samples)))
    centered = np.array([float(value - samples[0]) for value in samples])
    expected_ac = np.abs(np.fft.rfft(centered) / len(centered))[1:]
    np.testing.assert_allclose(actual_y[1:], expected_ac, rtol=1e-14, atol=0)
    np.testing.assert_array_equal(item.getOriginalDataset()[0], x)
    np.testing.assert_array_equal(item.getOriginalDataset()[1], y)


@pytest.mark.parametrize("x_dtype,y_dtype", [
    (np.longdouble, float), (float, np.longdouble),
    (np.longdouble, np.longdouble), (object, np.longdouble),
    (np.longdouble, object), (object, object),
])
@pytest.mark.parametrize("uniform", [False, True])
def test_extended_and_mixed_native_fft_ordinary_samples(x_dtype, y_dtype, uniform):
    x = np.array([0, 1, 2 if uniform else 3], dtype=np.longdouble).astype(x_dtype)
    y = np.array([1, 2, 4], dtype=np.longdouble).astype(y_dtype)
    item = NativePlotDataItem(x, y)
    item.setFftMode(True)
    samples = [1., 2., 4.] if uniform else [1., 2.5, 4.]
    np.testing.assert_allclose(item.getData()[1], np.abs(np.fft.rfft(samples) / 3), rtol=1e-14)


@pytest.mark.parametrize("values", [
    [1., np.nan, 2.], [1., np.inf, 2.], [np.inf, 1., 2.],
    [1., 2., -np.inf], [np.inf, np.inf, 2.], [np.inf, -np.inf, 2.],
])
def test_extended_interpolation_retains_missing_and_infinite_sample_semantics(values):
    from qplot.windows._native_transforms import _interpolate_fft_samples

    x = np.array([0., 1., 3.])
    targets = np.array([0., .5, 1., 1.5, 2., 3.])
    expected = np.interp(targets, x, values)
    actual = _interpolate_fft_samples(targets, x, np.asarray(values, dtype=np.longdouble))
    np.testing.assert_array_equal(actual, expected)


def test_extended_interpolation_finite_opposite_extremes():
    from qplot.windows._native_transforms import _interpolate_fft_samples

    extreme = np.finfo(np.longdouble).max
    x = np.array([0., 2.], dtype=np.longdouble)
    y = np.array([-extreme, extreme], dtype=np.longdouble)
    targets = np.array([0., .5, 1., 1.5, 2.], dtype=np.longdouble)
    actual = _interpolate_fft_samples(targets, x, y)
    np.testing.assert_array_equal(actual, [-extreme, -extreme / 2, 0, extreme / 2, extreme])


@pytest.mark.parametrize("middle", [0., -5e307])
@pytest.mark.parametrize("reverse", [False, True])
def test_extended_coordinate_rebase_keeps_wide_span_scaling(middle, reverse):
    x = np.array([-1e308, middle, 1e308], dtype=np.longdouble)
    y = np.array([1., 2., 4.])
    if reverse:
        x, y = x[::-1], y[::-1]
    item = NativePlotDataItem(x, y)
    item.setFftMode(True)
    expected = [1., 2., 4.] if middle == 0. else [1., 8. / 3., 4.]
    np.testing.assert_allclose(item.getData()[0], [0., (1. / 1e308) / 3.], rtol=1e-14, atol=0)
    np.testing.assert_allclose(item.getData()[1], np.abs(np.fft.rfft(expected) / 3), rtol=1e-14)
