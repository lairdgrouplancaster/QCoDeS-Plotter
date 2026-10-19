"""Finite QCoDeS coordinates can have gaps beyond float64's range."""

from fractions import Fraction
from functools import partial

import numpy as np
import pytest

from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.operation_registry import OperationCall
from qplot.tools.plot_tools import differentiate
from qplot.tools.worker import loader
from qplot.windows._native_transforms import NativePlotDataItem
from tests.test_derivative_numeric_ranges import _measurement, _source_state


def _gradient_oracle(coordinates, values):
    x, y = [[Fraction(float(value)) for value in array] for array in (coordinates, values)]
    secants = [(right - left) / (stop - start)
               for start, stop, left, right in zip(x[:-1], x[1:], y[:-1], y[1:], strict=True)]
    interior = []
    for index in range(1, len(x) - 1):
        before, after = x[index] - x[index - 1], x[index + 1] - x[index]
        interior.append((after * secants[index - 1] + before * secants[index]) / (before + after))
    return np.array(list(map(float, [secants[0], *interior, secants[-1]])))


@pytest.mark.parametrize(("surface", "case"), [
    ("line", "two-point"),
    *[(surface, case) for surface in ("line", "x", "y")
      for case in ("nonuniform", "small-signal")],
])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
def test_recorded_finite_coordinate_gaps_support_derivatives(
    tmp_path, surface, reverse, mixed, case,
):
    x = np.array([-1e308, 1e308])
    if case != "two-point":
        x = np.array([-1.5e308, -1e308, 1e308, 1.25e308])
    y = x.copy() if case != "small-signal" else np.array([0., 1., 3., 4.])
    if reverse:
        x, y = x[::-1], y[::-1]
    expected = _gradient_oracle(x, y)
    path, run_id = _measurement(tmp_path, x, y, heatmap=surface != "line", mixed=mixed)
    before = _source_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        params = {parameter.name: parameter for parameter in dataset.get_parameters()}
        axes = ({"x": "x"} if surface == "line" else {"x": "x", "y": "fixed"}
                if surface == "x" else {"x": "fixed", "y": "x"})
        axis = "y" if surface == "y" else "x"
        operation = OperationCall("differentiate", partial(differentiate, axis),
                                  derivative_axis=axis, cooperative=True)
        worker = loader(dataset.cache, params["signal"], params, axes, operations=[operation])
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert errors == []
        assert finished == [True]
        if surface == "line":
            np.testing.assert_array_equal(worker.axis_data["y"], expected)
            native = NativePlotDataItem(x=x, y=y, derivativeMode=True)
            np.testing.assert_array_equal(native.getData()[1][[0, -1]], expected[[0, -1]])
        else:
            expected = expected[::-1] if reverse else expected
            actual = worker.dataGrid if surface == "x" else worker.dataGrid.T
            np.testing.assert_array_equal(actual, np.tile(expected, (2, 1)))
    finally:
        dataset.conn.close()
    assert _source_state(path) == before


@pytest.mark.parametrize("dtype", [float, np.int64, object])
def test_overflowing_coordinate_edge_keeps_small_signal_and_missing_samples(dtype):
    x = np.array([-1.5e308, -1e308, 1e308, 1.25e308])
    signal = np.array([0, 1, 3, 4], dtype=dtype)
    original = signal.copy()
    actual = differentiate("x", {"x": x, "y": signal, "z": None})["y"]
    np.testing.assert_array_equal(actual, _gradient_oracle(x, signal))
    np.testing.assert_array_equal(signal, original)
    missing = signal.astype(object)
    missing[1] = np.nan
    actual = differentiate("x", {"x": x, "y": missing, "z": None})["y"]
    assert np.all(np.isnan(actual[:3]))
    assert actual[-1] == _gradient_oracle(x, signal)[-1]


@pytest.mark.parametrize("invalid", [np.inf, -np.inf, np.nan])
@pytest.mark.parametrize("dtype", [float, object])
def test_nonfinite_coordinate_still_rejected(invalid, dtype):
    with pytest.raises(ValueError, match="coordinates must be finite"):
        differentiate("x", {"x": np.array([0, invalid], dtype=dtype),
                            "y": np.array([0., 1.]), "z": None})


@pytest.mark.parametrize("axis", ["x", "y"])
def test_overflowing_coordinate_repairs_remain_cancellable(axis):
    x = np.array([-1e308, 1e308])
    z = np.tile(x, (4096, 1))
    data = {"x": x, "y": np.arange(len(z)), "z": z}
    if axis == "y":
        data = {"x": np.arange(len(z)), "y": x, "z": z.T}
    original = data["z"].copy()
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 4

    with pytest.raises(InterruptedError, match="cancelled"):
        differentiate(axis, data, cancelled_callback=cancelled)
    np.testing.assert_array_equal(data["z"], original)
