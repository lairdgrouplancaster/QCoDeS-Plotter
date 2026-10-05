"""Real acquired samples at float64 limits, using the visible native controls."""

from fractions import Fraction

import numpy as np
import pytest

from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.windows.test_numerical_stability import (
    measured_plots as _measured_plots_fixture,
)
from tests.windows.test_plot_integration import wait_for

measured_plots = _measured_plots_fixture
no_callback_errors = _no_callback_errors_fixture


@pytest.mark.parametrize("measured_plots", [
    (np.array([-1e308, 1e308]), np.array([-1e308, 1e308]), 1),
    (np.array([-1e308, 1e308]), np.array([0., 1.]), 1),
    (np.array([0., 1e308]), np.array([-1e308, 1e308]), 1),
], indirect=True)
@pytest.mark.parametrize("control", ["derivativeCheck", "phasemapCheck"])
def test_native_secant_preserves_finite_quotient(
    measured_plots, no_callback_errors, control,
):
    _window, (host,), x, y = measured_plots
    expected = np.array([
        float((Fraction(float(b)) - Fraction(float(a))) /
              (Fraction(float(d)) - Fraction(float(c))))
        for a, b, c, d in zip(y[:-1], y[1:], x[:-1], x[1:], strict=True)
    ])
    click_control(host, control)
    for refresh in (False, True):
        if refresh:
            host.refreshWindow(force=True)
            wait_for(lambda: not host.worker.running)
            host.monitor.stop()
        np.testing.assert_allclose(host.line.getData()[1], expected, rtol=2e-15, atol=0)
        assert np.all(np.isfinite(host.line.getData()[1]))
        np.testing.assert_array_equal(host.line.getOriginalDataset()[0], x)
        np.testing.assert_array_equal(host.line.getOriginalDataset()[1], y)


@pytest.mark.parametrize("measured_plots", [
    (np.arange(4.), np.full(4, 1e308), 1),
    (np.arange(4.), np.array([1e308, -1e308, 1e308, -1e308]), 1),
    (np.arange(4.), np.full(4, np.finfo(float).max), 1),
], indirect=True)
def test_native_fft_normalizes_before_overflow(measured_plots, no_callback_errors):
    _window, (host,), x, y = measured_plots
    expected = np.zeros(3)
    expected[0 if np.all(y == y[0]) else 2] = abs(y[0])
    click_control(host, "fftCheck")
    assert host.plot.ctrl.fftCheck.isChecked()
    np.testing.assert_allclose(host.line.getData()[1], expected, rtol=2e-15, atol=0)
    assert np.all(np.isfinite(host.line.getData()[1]))
    np.testing.assert_array_equal(host.line.getOriginalDataset()[1], y)


@pytest.mark.parametrize("measured_plots", [
    (np.array([-1e308, 0., 1e308]), np.array([1., 1., 1.]), 1),
    (np.array([-1e308, 1e308]), np.array([1., 1.]), 1),
    (np.arange(4.) * 4e-309, np.ones(4), 1),
], indirect=True)
def test_native_fft_wide_finite_coordinate_span(measured_plots, no_callback_errors):
    _window, (host,), x, y = measured_plots
    click_control(host, "fftCheck")
    assert host.plot.ctrl.fftCheck.isChecked()
    # Reciprocal spacing and sample count must also be combined safely.
    span = Fraction(float(x[-1])) - Fraction(float(x[0]))
    expected_x = np.array([float(Fraction(k * (len(x)-1), len(x)) / span)
                           for k in range(len(x)//2+1)])
    np.testing.assert_allclose(host.line.getData()[0], expected_x, rtol=2e-15, atol=0)
    np.testing.assert_array_equal(host.line.getData()[1], np.r_[1., np.zeros(len(x)//2)])
