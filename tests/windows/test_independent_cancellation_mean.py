"""Keep measured cancellation residuals when removing a source mean."""

from fractions import Fraction

import numpy as np
import pytest

from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.windows.test_numerical_stability import (
    measured_plots as _measured_plots_fixture,
)

measured_plots = _measured_plots_fixture
no_callback_errors = _no_callback_errors_fixture


def centered_oracle(values):
    exact = [Fraction(float(value)) for value in values]
    mean = sum(exact) / len(exact)
    return np.array([float(value - mean) for value in exact])


@pytest.mark.parametrize("measured_plots", [
    (np.arange(3.), np.array([1e16, 1., -1e16]), 1),
    (np.arange(4.), np.array([0., 1e308, 1., -1e308]), 1),
], indirect=True)
def test_native_mean_preserves_measured_cancellation_residual(
    measured_plots, no_callback_errors,
):
    _window, (host,), _x, values = measured_plots
    click_control(host, "subtractMeanCheck")
    np.testing.assert_array_equal(host.line.getData()[1], centered_oracle(values))
    np.testing.assert_array_equal(host.line.getOriginalDataset()[1], values)


@pytest.mark.parametrize("measured_plots", [
    (np.arange(3.), np.tile([1e16, 1., -1e16], (2, 1)), 1),
    (np.arange(4.), np.tile([0., 1e16, 1., -1e16], (2, 1)), 1),
], indirect=True)
def test_heatmap_row_mean_preserves_measured_cancellation_residual(
    measured_plots, no_callback_errors,
):
    _window, (host,), _x, grid = measured_plots
    operation_option(host, "Subtract Row Mean").input.setChecked(True)
    assert apply_operations(host)[1:] == ([True], [])
    np.testing.assert_array_equal(host.dataGrid, np.array([centered_oracle(row) for row in grid]))


def test_mixed_sign_mean_cancels_during_compensated_sum():
    from qplot.tools.plot_tools import _center_float_samples

    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls == 4

    values = np.r_[-1., np.ones(2048)]
    with pytest.raises(InterruptedError, match="cancelled"):
        _center_float_samples(values, cancelled_callback=cancelled)


def test_mixed_sign_mean_cancels_during_exact_fallback(monkeypatch):
    import qplot.tools.plot_tools as plot_tools

    original_fraction = Fraction
    calls = 0

    def fraction(*args):
        nonlocal calls
        if args:
            calls += 1
        return original_fraction(*args)

    monkeypatch.setattr(plot_tools, "Fraction", fraction)
    values = np.r_[np.full(2048, 1e308), -1e308]
    with pytest.raises(InterruptedError, match="cancelled"):
        plot_tools._center_float_samples(values, cancelled_callback=lambda: calls >= 1024)
    assert calls == 1024
