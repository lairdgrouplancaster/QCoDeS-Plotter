"""FFT resampling keeps narrow intervals and unrepresentable target positions."""

import csv
from decimal import Decimal, localcontext
from fractions import Fraction
from types import SimpleNamespace

import numpy as np
import pytest
from pyqtgraph.exporters import CSVExporter

from qplot.windows import _native_transforms as transforms
from qplot.windows._native_transforms import NativePlotDataItem, _resample_fft_samples
from tests.windows.test_native_fft_coordinates import fft_plots as _fft_plots_fixture
from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.windows.test_plot_integration import wait_for

fft_plots = _fft_plots_fixture
no_callback_errors = _no_callback_errors_fixture


def _fraction(value):
    return (Fraction(int(value)) if isinstance(value, (int, np.integer, bool, np.bool_))
            else Fraction(float(value)))


def _spectrum(x, y):
    """Exact interpolation and five-point DFT roots, independent of FFT code."""
    x, y = [list(map(_fraction, values)) for values in (x, y)]
    if x[0] > x[-1]:
        x, y = x[::-1], y[::-1]
    targets = [x[0] + (x[-1] - x[0]) * i / 4 for i in range(5)]
    values = []
    for target in targets:
        segment = min(max(sum(point <= target for point in x) - 1, 0), 3)
        left, right = x[segment:segment + 2]
        before, after = y[segment:segment + 2]
        values.append(before + (after - before) * (target - left) / (right - left))
    with localcontext() as context:
        context.prec = 100
        root = Decimal(5).sqrt()
        cosine = [Decimal(1), (root - 1) / 4, -(root + 1) / 4,
                  -(root + 1) / 4, (root - 1) / 4]
        sine = [Decimal(0), (10 + 2 * root).sqrt() / 4, (10 - 2 * root).sqrt() / 4,
                -(10 - 2 * root).sqrt() / 4, -(10 + 2 * root).sqrt() / 4]
        samples = [Decimal(value.numerator) / Decimal(value.denominator) for value in values]
        magnitudes = []
        for frequency in range(3):
            real = sum(value * cosine[(frequency * i) % 5] for i, value in enumerate(samples))
            imaginary = sum(value * sine[(frequency * i) % 5] for i, value in enumerate(samples))
            magnitudes.append(float((real * real + imaginary * imaginary).sqrt() / 5))
    return [float(Fraction(4 * i, 5) / (x[-1] - x[0])) for i in range(3)], magnitudes


_BOUND = 2**54
_SHIFTED_LEFT, _SHIFTED_RIGHT = -_BOUND + 2, 3 * _BOUND + 8
_MIDPOINT = int(float(Fraction(_SHIFTED_LEFT + _SHIFTED_RIGHT, 2)))
_CASES = [
    (np.array([-_BOUND, -2, 1, 3, _BOUND], dtype=dtype), np.array([0., 0., 3., 0., 0.]))
    for dtype in (np.float64, np.int64)
] + [
    (np.array([-_BOUND, -2, 1, 3, _BOUND + 4], dtype=dtype), np.array([0., 0., 3., 0., 0.]))
    for dtype in (np.float64, np.int64)
] + [
    (np.array([_SHIFTED_LEFT, _MIDPOINT - 4, _MIDPOINT, _MIDPOINT + 4, _SHIFTED_RIGHT],
              dtype=dtype), np.array([0., 0., 3., 0., 0.]))
    for dtype in (np.float64, np.int64)
] + [
    (2.**30 + np.array([0., 1., 2., 3., 7.]) * 2.**-22, np.array([0., 0., 3., 0., 0.])),
    (np.array([-8., -2., 1., 3., 8.]), np.array([0., 0., 3., 0., 0.])),
]


@pytest.mark.parametrize("fft_plots", [
    {"x": x, "y": y, "scalar": scalar}
    for case, (x, y) in enumerate(_CASES) for scalar in (False, True)
    if not (case == 6 and scalar)  # Scalar acquisition rounds away these small offsets.
], indirect=True)
def test_narrow_recorded_intervals_through_fft_refresh_csv_and_reset(
    fft_plots, no_callback_errors, tmp_path,
):
    _window, plots, _coordinates, _values = fft_plots
    for index, plot in enumerate(plots):
        plot.line.setDynamicRangeLimit(None)
        x, y = [values.copy() for values in plot.line.getOriginalDataset()]
        expected_x, expected_y = _spectrum(x, y)
        click_control(plot, "fftCheck")
        assert plot.plot.ctrl.fftCheck.isChecked()
        for refresh in (False, True):
            if refresh:
                plot.refreshPlotButton.click()
                wait_for(lambda plot=plot: not plot.worker.running)
                plot.monitor.stop()
            for line in (plot.line, plot.line.curve):
                actual_x, actual_y = line.getData()
                np.testing.assert_allclose(actual_x, expected_x, rtol=3e-15, atol=0)
                np.testing.assert_allclose(actual_y, expected_y, rtol=3e-15, atol=0)
        destination = tmp_path / f"raw-{index}.csv"
        assert plot._write_line_csv_stage(str(destination), CSVExporter(plot.plot))
        with destination.open(newline="") as stream:
            rows = list(csv.reader(stream))[1:]
        for column, recorded in enumerate((x, y)):
            if recorded.dtype.kind in "iu":
                assert [int(Decimal(row[column])) for row in rows] == recorded.tolist()
            else:
                np.testing.assert_array_equal([float(row[column]) for row in rows], recorded)
        click_control(plot, "fftCheck")
        for actual, expected in zip(plot.line.getOriginalDataset(), (x, y), strict=True):
            np.testing.assert_array_equal(actual, expected)
        for actual, expected in zip(plot.line.getData(), (x, y), strict=True):
            np.testing.assert_array_equal(actual, expected)


@pytest.mark.parametrize("dtype", [np.int64, object])
@pytest.mark.parametrize("coordinates", [
    [-_BOUND, -_BOUND + 1, -2, 1, _BOUND],
    [-_BOUND, -2, 1, _BOUND - 1, _BOUND],
    [-_BOUND, -7, -3, 1, _BOUND],
])
def test_rounded_coordinate_groups_preserve_endpoints_and_brackets(dtype, coordinates):
    x = np.asarray(coordinates, dtype=dtype)
    y = np.array([1., 3., 0., 3., 2.])
    expected_x, expected_y = _spectrum(x, y)
    for raw_x, raw_y in ((x, y), (x[::-1], y[::-1])):
        actual_x, actual_y = NativePlotDataItem._fourierTransform(SimpleNamespace(), raw_x, raw_y)
        np.testing.assert_allclose(actual_x, expected_x, rtol=3e-15, atol=0)
        np.testing.assert_allclose(actual_y, expected_y, rtol=3e-15, atol=0)


def test_boolean_samples_in_narrow_coordinate_brackets():
    x = np.array([-_BOUND, -2, 1, 3, _BOUND], dtype=float)
    y = np.array([False, False, True, False, False])
    actual_x, actual_y = NativePlotDataItem._fourierTransform(SimpleNamespace(), x, y)
    expected_x, expected_y = _spectrum(x, y)
    np.testing.assert_allclose(actual_x, expected_x, rtol=3e-15, atol=0)
    np.testing.assert_allclose(actual_y, expected_y, rtol=3e-15, atol=0)


def test_dense_exact_grid_retains_vectorized_interpolation(monkeypatch):
    x = np.arange(1025, dtype=float)
    even = np.arange(2, len(x) - 2, 2)
    x[even], x[even + 1] = even - 2.**-15, even + 2.**-15
    y = np.sin(x)

    def unexpected_scalar_conversion(_value):
        pytest.fail("Accurate narrow-bracket targets should stay vectorized")

    monkeypatch.setattr(transforms, "_fft_sample_ratio", unexpected_scalar_conversion)
    actual = _resample_fft_samples(x, y, x)
    expected = np.interp(np.linspace(x[0], x[-1], len(x)), x, y)
    np.testing.assert_array_equal(actual, expected)


@pytest.mark.parametrize("count", [1025, 65537])
def test_sparse_narrow_interval_recovers_only_bounded_target_work(monkeypatch, count):
    x = np.linspace(-float(_BOUND), float(_BOUND), count)
    middle = count // 2
    x[middle - 1:middle + 2] = [-2., 1., 3.]
    y = np.zeros(count)
    y[middle] = 3.
    original_ratio = transforms._fft_sample_ratio
    calls = []

    def counted_ratio(value):
        calls.append(value)
        return original_ratio(value)

    monkeypatch.setattr(transforms, "_fft_sample_ratio", counted_ratio)
    _frequencies, spectrum = NativePlotDataItem._fourierTransform(SimpleNamespace(), x, y)
    np.testing.assert_allclose(spectrum, 2 / count, rtol=3e-15, atol=0)
    assert 0 < len(calls) <= count.bit_length() + 6


_CENTER = float(Fraction(2, 3))


@pytest.mark.parametrize("fft_plots", [
    {"x": np.array([0., _CENTER - 2 * width, _CENTER - width / 2,
                    _CENTER + width / 2, _CENTER + 2 * width, .9, 1.]) * scale,
     "y": np.array([0., 0., -1., 1. + residual, 0., 0., 0.])}
    for scale in (1., 2.**-1020)
    for width, residual in [(2.**-27, 0.), (2.**-22, 0.), (2.**-20, 0.),
                            (2.**-20, 2.**-24), (2.**-20, 2.**-23), (2.**-20, 2.**-20)]
] + [
    {"x": np.array([0., .1, _CENTER, _CENTER + 2.**-20,
                    _CENTER + 2.**-19, .9, 1.]) * scale,
     "y": np.array([0., 0., 0., 1., 0., 0., 0.])}
    for scale in (1., 2.**-1020)
] + [
    {"x": np.array([0., .1, _CENTER - 2.**-43,
                    _CENTER - 2.**-43 + 2.**-20,
                    _CENTER - 2.**-43 + 2.**-19, .9, 1.]) * scale,
     "y": np.array([0., 0., 0., 1., 0., 0., 0.])}
    for scale in (1., 2.**-1020)
], indirect=True)
def test_exact_target_keeps_small_cancellation_signal(fft_plots, no_callback_errors):
    _window, plots, _coordinates, _values = fft_plots
    for plot in plots:
        plot.line.setDynamicRangeLimit(None)
        x, y = plot.line.getOriginalDataset()
        if x[0] > x[-1]:
            x, y = x[::-1], y[::-1]
        exact = list(map(_fraction, x))
        target = exact[0] + (exact[-1] - exact[0]) * Fraction(4, 6)
        before, after = map(_fraction, y[2:4])
        interpolated = before + (after - before) * (target - exact[2]) / (exact[3] - exact[2])
        # All other uniformly resampled cells are zero: each DFT magnitude
        # equals the absolute impulse divided by the seven acquired samples.
        expected = float(abs(interpolated) / 7)
        assert expected > 0
        click_control(plot, "fftCheck")
        assert plot.plot.ctrl.fftCheck.isChecked()
        np.testing.assert_allclose(plot.line.getData()[1], expected, rtol=3e-15, atol=0)
        np.testing.assert_allclose(plot.line.curve.getData()[1], expected, rtol=3e-15, atol=0)


_SIX_CENTER = np.linspace(0., 1., 6)[3]


@pytest.mark.parametrize("fft_plots", [
    {"x": np.array([0., _SIX_CENTER - 2.**-19, _SIX_CENTER - 2.**-20,
                    _SIX_CENTER, _SIX_CENTER + 2.**-20, 1.]) * scale,
     "y": np.array([0., 0., 1., 0., 0., 0.])}
    for scale in (1., 2.**-1020)
], indirect=True)
def test_uncertain_boundary_checks_the_changing_neighbour(fft_plots, no_callback_errors):
    _window, plots, _coordinates, _values = fft_plots
    for plot in plots:
        plot.line.setDynamicRangeLimit(None)
        x, _y = plot.line.getOriginalDataset()
        if x[0] > x[-1]:
            x = x[::-1]
        exact = list(map(_fraction, x))
        target = exact[0] + (exact[-1] - exact[0]) * Fraction(3, 5)
        expected = float(abs((exact[3] - target) / (exact[3] - exact[2])) / 6)
        assert expected > 0
        click_control(plot, "fftCheck")
        assert plot.plot.ctrl.fftCheck.isChecked()
        np.testing.assert_allclose(plot.line.getData()[1], expected, rtol=3e-15, atol=0)
        np.testing.assert_allclose(plot.line.curve.getData()[1], expected, rtol=3e-15, atol=0)


@pytest.mark.skipif(np.finfo(np.longdouble).nmant <= np.finfo(float).nmant,
                    reason="Platform has no wider longdouble mantissa")
@pytest.mark.parametrize("large", [False, True])
def test_selected_extended_samples_keep_their_precision_and_range(large):
    coordinates = np.array([-_BOUND, -2, 1, 3, _BOUND + 4], dtype=float)
    anchor = np.longdouble(2) ** (1050 if large else 0)
    increment = np.spacing(anchor) * 4
    values = np.full(5, anchor, dtype=np.longdouble)
    values[2] += increment
    actual = _resample_fft_samples(coordinates, values, coordinates)
    assert actual.dtype == values.dtype
    assert actual[2] == anchor + increment / 2
    assert actual[0] == actual[-1] == anchor
