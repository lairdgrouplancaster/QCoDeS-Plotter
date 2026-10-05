"""Finite nonuniform stencils checked against exact Lagrange polynomials."""

from fractions import Fraction

import numpy as np
import pytest

from qplot.tools.plot_tools import differentiate


def _lagrange_derivative_at_middle(x, y):
    x = [Fraction(value.item()) for value in x]
    y = [Fraction(value.item()) for value in y]
    result = Fraction()
    for index in range(3):
        others = [other for other in range(3) if other != index]
        numerator = sum((x[1] - x[other] for other in others), Fraction())
        denominator = (x[index] - x[others[0]]) * (x[index] - x[others[1]])
        result += y[index] * numerator / denominator
    return float(result)


@pytest.mark.parametrize("dtype", [np.int64, np.float64])
@pytest.mark.parametrize("scale", [1., 1e-100, 1e100])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("axis", ["line", "x", "y"])
def test_finite_cancellation_in_line_and_heatmap_derivatives(dtype, scale, reverse, axis):
    x = np.array([0., 1., 3.]) * scale
    y = np.array([0, 10**16, -3*10**16 + 4], dtype=dtype)
    if reverse:
        x, y = x[::-1], y[::-1]
    expected = _lagrange_derivative_at_middle(x, y)
    data = {"x": x, "y": y, "z": None}
    if axis != "line":
        z = np.tile(y, (2, 1))
        data = ({"x": x, "y": np.arange(2), "z": z} if axis == "x"
                else {"x": np.arange(2), "y": x, "z": z.T})
    originals = {name: value.copy() for name, value in data.items() if value is not None}
    result = differentiate("x" if axis == "line" else axis, data)
    observed = (result["y"][1] if axis == "line" else result["z"][:, 1] if axis == "x"
                else result["z"][1, :])
    np.testing.assert_array_equal(observed, np.full(np.shape(observed), expected))
    for name, original in originals.items():
        np.testing.assert_array_equal(data[name], original)


def test_finite_cancellation_recovery_remains_cancellable():
    x = np.array([0., 1., 3.])
    z = np.tile([0., 1e16, -3e16 + 4], (4096, 1))
    original = z.copy()
    calls = []

    def cancelled():
        calls.append(True)
        return len(calls) >= 4

    with pytest.raises(InterruptedError, match="cancelled"):
        differentiate("x", {"x": x, "y": np.arange(len(z)), "z": z},
                      cancelled_callback=cancelled)
    np.testing.assert_array_equal(z, original)
