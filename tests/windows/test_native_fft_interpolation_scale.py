"""Finite FFT resampling survives unrepresentable intermediate slopes."""

import csv
from decimal import Decimal, localcontext
from fractions import Fraction

import numpy as np
import pytest
from pyqtgraph.exporters import CSVExporter

from qplot.windows._native_transforms import _interpolate_fft_samples
from tests.windows.test_native_fft_coordinates import fft_plots as _fft_plots_fixture
from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.windows.test_plot_integration import wait_for

fft_plots = _fft_plots_fixture
no_callback_errors = _no_callback_errors_fixture


def _interpolation(targets, x, y):
    coordinates, values = [list(map(Fraction, map(float, v))) for v in (x, y)]
    output = []
    for target in map(Fraction, map(float, targets)):
        segment = next(i for i in range(len(x) - 1)
                       if coordinates[i] <= target <= coordinates[i + 1])
        left, right = coordinates[segment:segment + 2]
        before, after = values[segment:segment + 2]
        output.append(before + (after - before) * (target - left) / (right - left))
    return output


def _spectrum(x, y):
    """Four-point DFT identities avoid FFT and trigonometric cancellation."""
    if x[0] > x[-1]:
        x, y = x[::-1], y[::-1]
    a, b, c, d = _interpolation(np.linspace(x[0], x[-1], 4), x, y)
    with localcontext() as context:
        context.prec = 100

        def decimal(value):
            return Decimal(value.numerator) / Decimal(value.denominator)

        ac = (decimal(a - c) ** 2 + decimal(b - d) ** 2).sqrt() / 4
    span = Fraction(float(x[-1])) - Fraction(float(x[0]))
    return (
        [float(Fraction(3 * k, 4) / span) for k in range(3)],
        [float(abs((a + b + c + d) / 4)), float(ac), float(abs((a - b + c - d) / 4))],
    )


_SCALES = [
    (2.**-20, 2.**1018),  # finite differences, overflowing interpolation slope
    (2.**900, 2.**-400),  # slope rounds to zero
    (2.**600, 5 * 2.**-476),  # nonzero subnormal slope loses one fifth of its value
    (1., 2.**1018),  # same amplitudes, representable slope
    (2.**-20, 1.),  # same coordinates, representable slope
]


@pytest.mark.parametrize("fft_plots", [
    {"x": np.array([0., 1., 2., 4.]) * sx,
     "y": np.array([0., 1., 2., 4.]) * sy, "scalar": scalar}
    for sx, sy in _SCALES for scalar in (False, True)
], indirect=True)
def test_scale_extremes_through_actual_fft_refresh_csv_and_reset(
    fft_plots, no_callback_errors, tmp_path,
):
    _window, plots, _x, _y = fft_plots
    # The fixture's second plot has the same recorded sweep in reverse order.
    for index, plot in enumerate(plots):
        plot.line.setDynamicRangeLimit(None)
        x, y = (values.copy() for values in plot.line.getOriginalDataset())
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
                assert np.all(np.isfinite(actual_y))
                np.testing.assert_allclose(actual_x, expected_x, rtol=3e-15, atol=0)
                np.testing.assert_allclose(actual_y, expected_y, rtol=3e-15, atol=0)
        # Native FFT is a display operation; CSV must keep acquired samples.
        destination = tmp_path / f"spectrum-{index}.csv"
        assert plot._write_line_csv_stage(str(destination), CSVExporter(plot.plot))
        with destination.open(newline="") as stream:
            rows = list(csv.reader(stream))[1:]
        np.testing.assert_array_equal(np.asarray(rows, dtype=float), np.column_stack((x, y)))
        click_control(plot, "fftCheck")
        np.testing.assert_array_equal(plot.line.getData()[0], x)
        np.testing.assert_array_equal(plot.line.getData()[1], y)
        np.testing.assert_array_equal(plot.line.getOriginalDataset()[0], x)
        np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], y)


@pytest.mark.parametrize("scale_x,scale_y", _SCALES)
@pytest.mark.parametrize("sign", [-1., 1.])
def test_signed_resampling_and_exact_endpoints(scale_x, scale_y, sign):
    x = np.array([0., 1., 2., 4.]) * scale_x
    y = np.array([0., 1., -1., 2.]) * scale_y * sign
    targets = np.array([0., .5, 1., 1.5, 2., 3., 4.]) * scale_x
    actual = _interpolate_fft_samples(targets, x, y)
    expected = [float(value) for value in _interpolation(targets, x, y)]
    assert np.all(np.isfinite(actual))
    np.testing.assert_allclose(actual, expected, rtol=3e-15, atol=0)
    np.testing.assert_array_equal(actual[[0, 2, 4, 6]], y)


@pytest.mark.parametrize("x,y,target", [
    ([0., 3 * 2.**-20], [0., 3 * 2.**1018], np.nextafter(0., 1.)),
    ([-2.**-30, 2.**-90], [2.**1018, 2.**-100], 0.),
    ([-2.**-90, 2.**-30], [2.**-100, 2.**1018], 0.),
])
def test_small_weights_keep_representable_large_amplitude_contributions(x, y, target):
    x, y = np.asarray(x), np.asarray(y)
    targets = np.array([x[0], target, x[-1]])
    expected = [float(value) for value in _interpolation(targets, x, y)]
    actual = _interpolate_fft_samples(targets, x, y)
    np.testing.assert_allclose(actual, expected, rtol=3e-15, atol=0)
    np.testing.assert_array_equal(actual[[0, -1]], y)


@pytest.mark.parametrize("values", [
    [0., 0., 0.], [1., 1., 1.], [-1., -1., -1.],
    [1., np.nan, 2.], [1., np.inf, 2.], [np.inf, 1., 2.],
    [1., 2., -np.inf], [np.inf, np.inf, 2.], [np.inf, -np.inf, 2.],
])
def test_ordinary_constant_and_missing_sample_controls(values):
    x = np.array([0., 1., 3.])
    targets = np.array([0., .5, 1., 1.5, 2., 3.])
    y = np.asarray(values)
    expected = np.interp(targets, x, y)
    actual = _interpolate_fft_samples(targets, x, y)
    np.testing.assert_array_equal(actual, expected)
