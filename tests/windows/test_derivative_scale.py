"""Finite derivatives survive extreme but valid QCoDeS coordinate scales."""

import numpy as np
import pytest

from qplot.tools.plot_tools import differentiate
from tests.windows.test_differentiation_integration import (
    apply_operations,
    exact_gradient,
    operation_option,
)
from tests.windows.test_differentiation_integration import (
    integer_array_plot as _integer_array_plot_fixture,
)

integer_array_plot = _integer_array_plot_fixture


@pytest.mark.parametrize("integer_array_plot", [
    (np.array([0., 1e-200, 3e-200]), np.array([0., 1e-200, 3e-200])),
    (np.array([3e-200, 1e-200, 0.]), np.array([3e-200, 1e-200, 0.])),
    (np.array([0., 1e200, 3e200]), np.array([0., 1e200, 3e200])),
    (np.array([3e200, 1e200, 0.]), np.array([3e200, 1e200, 0.])),
    (np.array([-1e308, 0., 1e308]), np.array([-1e308, 0., 1e308])),
    (np.array([0., 1e308, 1.5e308]), np.array([-1e308, 1e308, 0.])),
    (np.array([1.5e308, 1e308, 0.]), np.array([0., 1e308, -1e308])),
], indirect=True)
def test_scaled_derivative_through_operations_controls(integer_array_plot):
    plot = integer_array_plot
    expected = exact_gradient(plot.axis_data["x"], plot.axis_data["y"])
    operation_option(plot, "dy/dx").input.click()
    _worker, finished, errors = apply_operations(plot)
    assert finished == [True] and errors == []
    np.testing.assert_allclose(plot.axis_data["y"], expected, rtol=2e-15, atol=0)
    np.testing.assert_allclose(plot.line.getData()[1], expected, rtol=2e-15, atol=0)


@pytest.mark.parametrize("coordinates", [
    np.array([-1e308, 0., 1e308]),
    np.array([1e308, 0., -1e308]),
    np.array([0., 1e-200, 3e-200, 6e-200]),
    np.array([0., 1e200, 3e200, 6e200]),
])
@pytest.mark.parametrize("axis", ["x", "y"])
def test_extreme_stencils_match_rational_oracle(coordinates, axis):
    values = coordinates.copy()
    rows = np.vstack([values, values / 2])
    grid = rows if axis == "x" else rows.T
    with np.errstate(over="raise", divide="raise", invalid="raise"):
        actual = differentiate(axis, {axis: coordinates, "z": grid})["z"]
    expected = np.vstack([exact_gradient(coordinates, row) for row in rows])
    np.testing.assert_allclose(actual, expected if axis == "x" else expected.T,
                               rtol=2e-15, atol=0)


def test_extreme_uniform_stencil_does_not_use_missing_centre():
    coordinates = np.array([-1e308, 0., 1e308])
    values = np.array([-1e308, np.nan, 1e308])
    actual = differentiate("x", {"x": coordinates, "y": values, "z": None})["y"]
    np.testing.assert_array_equal(actual, [np.nan, 1., np.nan])


def test_extreme_uniform_spacing_with_exact_integer_samples():
    coordinates = np.array([-1e308, 0., 1e308])
    values = np.array([-1, 0, 1])
    with np.errstate(over="raise", invalid="raise"):
        actual = differentiate("x", {"x": coordinates, "y": values, "z": None})["y"]
    np.testing.assert_array_equal(actual, exact_gradient(coordinates, values))
