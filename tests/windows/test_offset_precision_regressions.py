"""Recorded offsets must not become averaging or Fourier artefacts."""

import math
from fractions import Fraction

import numpy as np
import pytest
from PyQt6 import QtCore, QtTest

from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_merged_trace_metadata import merge
from tests.windows.test_native_averaging import click_average
from tests.windows.test_native_fft_coordinates import fft_plots as _fft_plots_fixture
from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.windows.test_plot_integration import wait_for

fft_plots = _fft_plots_fixture
no_callback_errors = _no_callback_errors_fixture


def _average_runs(first, second, dtype):
    return {"x": np.arange(4), "other_x": np.arange(4),
            "y": np.full(4, first, dtype=dtype),
            "other_y": np.full(4, second, dtype=dtype)}


@pytest.mark.parametrize("fft_plots", [
    _average_runs(2**53 + 1, 2**53 + 2, np.int64),
    _average_runs(-(2**53 + 1), -(2**53 + 2), np.int64),
    _average_runs(2**63 + 1, 2**63 + 2048, np.uint64),
], indirect=True)
def test_stored_integer_averages_round_only_after_accumulation(fft_plots, no_callback_errors):
    window, (host, other), _x, _y = fft_plots
    originals = [plot.line.getOriginalDataset()[1].copy() for plot in (host, other)]
    expected = float(sum(Fraction(int(values[0])) for values in originals) / 2)
    merge(window, host, other, x_axis="Bottom", y_axis="Left")
    click_average(host)
    for refresh in (False, True):
        if refresh:
            host.refreshWindow(force=True)
            wait_for(lambda: not host.worker.running)
            host.monitor.stop()
        count, average = next(iter(host.plot.avgCurves.values()))
        assert count == 2
        # An allclose tolerance would hide this one-ULP final-result error.
        np.testing.assert_array_equal(average.getData()[1], np.full(4, expected))
        for line, original in zip(host.lines.values(), originals, strict=True):
            assert line.getOriginalDataset()[1].dtype == original.dtype
            np.testing.assert_array_equal(line.getOriginalDataset()[1], original)
        host.plot.recomputeAverages()


_FFT_CASES = [
    {"x": np.arange(3), "y": np.array([1e16, 1e16 + 2, 1e16 + 4])},
    {"x": np.arange(3), "y": np.array([-1e16, -1e16 + 2, -1e16 + 4])},
    {"x": np.array([0., 1., 3.]), "y": np.array([1e16, 1e16 + 2, 1e16 + 4])},
    {"x": np.array([0., 1., 3.]), "y": np.array([-1e16, -1e16 + 2, -1e16 + 4])},
    {"x": np.arange(5), "y": np.full(5, 1e16)},
    {"x": np.arange(5), "y": np.full(5, np.finfo(float).max)},
]


def _analytic_spectrum(x, y):
    """Exact DC and closed-form three-point DFT, independent of FFT code."""
    anchor = Fraction(float(y[0]))
    if len(y) == 5:
        assert np.all(y == y[0])
        return [abs(float(anchor)), 0., 0.]
    a, b, c = (Fraction(float(value)) - anchor for value in y)
    if x[1] == 1 and x[2] == 3:
        # The middle uniform coordinate is 1.5, one quarter of the way
        # through the final interval in this independently chosen dataset.
        b = b + (c - b) / 4
    dc = abs(float(anchor + (a + b + c) / 3))
    ac = math.sqrt(float((a*a + b*b + c*c - a*b - b*c - c*a) / 9))
    return [dc, ac]


@pytest.mark.parametrize("fft_plots", _FFT_CASES, indirect=True)
def test_stored_float_offsets_do_not_leak_into_fourier_bins(fft_plots, no_callback_errors):
    _window, plots, x, y = fft_plots
    expected = _analytic_spectrum(x, y)
    for plot in plots:  # Paired increasing and decreasing acquired sweeps.
        original = tuple(value.copy() for value in plot.line.getOriginalDataset())
        plot.line.setDynamicRangeLimit(None)
        click_control(plot, "fftCheck")
        for refresh in (False, True):
            if refresh:
                plot.refreshWindow(force=True)
                wait_for(lambda plot=plot: not plot.worker.running)
                plot.monitor.stop()
            for line in (plot.line, plot.line.curve):
                np.testing.assert_allclose(line.getData()[1], expected, rtol=2e-15, atol=0)
                assert line.getData()[1][0] == expected[0]
            for actual, recorded in zip(plot.line.getOriginalDataset(), original, strict=True):
                np.testing.assert_array_equal(actual, recorded)
        click_control(plot, "logXCheck")
        np.testing.assert_allclose(plot.line.getData()[1], expected[1:], rtol=2e-15, atol=0)
        click_control(plot, "logXCheck")
        click_control(plot, "fftCheck")
        np.testing.assert_array_equal(plot.line.getData()[1], original[1])


@pytest.mark.parametrize("fft_plots", [
    {"x": np.array([0, 1, 3]), "y": np.array([0, 10**16, -3*10**16 + 4])},
    {"x": np.array([0., 1., 3.]), "y": np.array([0., 1e16, -3e16 + 4])},
], indirect=True)
def test_operation_preserves_finite_derivative_cancellation(fft_plots, no_callback_errors):
    _window, plots, _x, _y = fft_plots
    for plot in plots:
        original = tuple(value.copy() for value in plot.line.getOriginalDataset())
        plot.line.setDynamicRangeLimit(None)
        option = operation_option(plot, "dy/dx")
        option.input.show()
        QtTest.QTest.mouseClick(option.input, QtCore.Qt.MouseButton.LeftButton)
        _worker, finished, errors = apply_operations(plot)
        assert finished == [True] and not errors
        # The interpolating quadratic has derivative 2/3 at coordinate 1,
        # in either acquisition direction. It must not round to zero.
        assert plot.line.getOriginalDataset()[1][1] == float(Fraction(2, 3))
        QtTest.QTest.mouseClick(option.input, QtCore.Qt.MouseButton.LeftButton)
        _worker, finished, errors = apply_operations(plot)
        assert finished == [True] and not errors
        for actual, recorded in zip(plot.line.getOriginalDataset(), original, strict=True):
            assert actual.dtype == recorded.dtype
            np.testing.assert_array_equal(actual, recorded)
