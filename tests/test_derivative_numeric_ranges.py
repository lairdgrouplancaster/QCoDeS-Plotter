"""Recorded finite stencils retain results across extreme numeric ranges."""

import hashlib
from decimal import Decimal
from fractions import Fraction
from functools import partial

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.operation_registry import OperationCall
from qplot.tools.plot_tools import differentiate, pass_filter
from qplot.tools.worker import loader
from qplot.windows._native_transforms import NativePlotDataItem


def _fraction(value):
    return Fraction(value.item() if isinstance(value, np.generic) else value)


def _derivative_oracle(x, y):
    """Differentiate the exact interpolating polynomial at each stencil."""
    x, y = list(map(_fraction, x)), list(map(_fraction, y))
    middle = Fraction()
    for index in range(3):
        other = [j for j in range(3) if j != index]
        coefficient = ((x[1] - x[other[0]]) + (x[1] - x[other[1]]))
        coefficient /= (x[index] - x[other[0]]) * (x[index] - x[other[1]])
        middle += coefficient * y[index]
    return np.array([float((y[1] - y[0]) / (x[1] - x[0])),
                     float(middle), float((y[2] - y[1]) / (x[2] - x[1]))])


def _measurement(tmp_path, x, signal, *, heatmap=False, mixed=False, integer_first=None):
    path = tmp_path / "d.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    exp = load_or_create_experiment("numeric ranges", sample_name="test")
    measurement = Measurement(exp=exp)
    measurement.register_custom_parameter("x", paramtype="array")
    if heatmap:
        measurement.register_custom_parameter("fixed", paramtype="array")
    measurement.register_custom_parameter(
        "signal", setpoints=("fixed", "x") if heatmap else ("x",), paramtype="array",
    )
    writer = None
    try:
        with measurement.run() as saver:
            writer = saver.dataset
            for row in range(2 if heatmap else 1):
                for index, (coordinate, value) in enumerate(zip(x, signal, strict=True)):
                    # Individual array records intentionally retain mixed dtypes.
                    x_dtype = np.int64 if mixed and coordinate == 0 else np.float64
                    y_dtype = np.int64 if mixed and value == 0 else np.float64
                    if integer_first == "x":
                        x_dtype = np.int64 if index == 0 else np.float64
                    elif integer_first == "signal":
                        y_dtype = np.int64 if index == 0 else np.float64
                    values = [("x", np.array([coordinate], dtype=x_dtype)),
                              ("signal", np.array([value], dtype=y_dtype))]
                    if heatmap:
                        values.append(("fixed", np.array([row], dtype=np.int64)))
                    saver.add_result(*values)
            run_id = saver.dataset.run_id
    finally:
        if writer is not None:
            writer.conn.close()
        exp.conn.close()
    return path, run_id


def _source_state(path):
    files = [path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-journal")]
    return [(item.name, item.stat().st_size, item.stat().st_mtime_ns,
             hashlib.sha256(item.read_bytes()).digest()) if item.exists() else (item.name, None)
            for item in files]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("surface", ["line", "x", "y"])
@pytest.mark.parametrize("case", ["weight-underflow", "mixed-edge", "mixed-centre", "clipped"])
def test_real_qcodes_derivative_extreme_finite_stencils(tmp_path, reverse, surface, case):
    x, signal = (np.array([0., 1e-200, 1e200]), np.array([0., 0., 1e308]))
    if case != "weight-underflow":
        x = np.array([-1e308, 0., 1e308])
        signal = np.array([-1e308, 1e308, 0.] if case == "mixed-edge"
                          else [-1e308, 0., 1e308])
    if case == "clipped":
        signal = np.array([-1.7e308, 1.7e308, 0.])
    if reverse:
        x, signal = x[::-1], signal[::-1]
    expected_signal = signal
    if case == "clipped":
        expected_signal = np.array([min(_fraction(value), Fraction(Decimal("1.6e308")))
                                    for value in signal], dtype=object)
    expected = _derivative_oracle(x, expected_signal)
    path, run_id = _measurement(
        tmp_path, x, signal, heatmap=surface != "line", mixed=case != "weight-underflow",
    )
    before = _source_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        params = {p.name: p for p in dataset.get_parameters()}
        axes = ({"x": "x"} if surface == "line" else {"x": "x", "y": "fixed"}
                if surface == "x" else {"x": "fixed", "y": "x"})
        derivative_axis = "y" if surface == "y" else "x"
        operation = OperationCall(
            "derivative", partial(differentiate, derivative_axis),
            derivative_axis=derivative_axis, cooperative=True,
        )
        operations = [operation]
        if case == "clipped":
            operations.insert(0, OperationCall(
                "Limit Maximum", partial(pass_filter, "low", Decimal("1.6e308")),
                cooperative=True,
            ))
        worker = loader(dataset.cache, params["signal"], params, axes, operations=operations)
        errors, finished = [], []
        worker.emitter.errorOccurred.connect(errors.append)
        worker.emitter.finished.connect(finished.append)
        worker.run()
        assert errors == []
        assert finished == [True]
        if surface == "line":
            actual = worker.axis_data["y"]
        else:
            # Heatmaps canonicalize their acquisition axis to increasing order.
            expected = expected[::-1] if reverse else expected
            actual = worker.dataGrid if surface == "x" else worker.dataGrid.T
            expected = np.tile(expected, (2, 1))
        np.testing.assert_array_equal(actual, expected)
        if case != "weight-underflow":
            source = worker.cache_data["signal"]
            assert source["x"].dtype == object
            assert source["signal"].dtype == object
    finally:
        dataset.conn.close()
    assert _source_state(path) == before


@pytest.mark.parametrize("mixed_column", ["x", "signal"])
@pytest.mark.parametrize("reverse", [False, True])
def test_real_mixed_integer_float_steps_in_operation_and_native_derivative(
    tmp_path, mixed_column, reverse,
):
    x, y = [0., 1., 2.], [2**53 + 1, float(2**53), float(2**53 + 2)]
    if mixed_column == "x":
        x, y = [2**53 + 1, float(2**53 + 2), float(2**53 + 4)], [0., 1., 3.]
    expected = _derivative_oracle(x, y)
    path, run_id = _measurement(tmp_path, x, y, integer_first=mixed_column)
    before = _source_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        params = {p.name: p for p in dataset.get_parameters()}
        worker = loader(dataset.cache, params["signal"], params, {"x": "x"})
        errors, finished = [], []
        worker.emitter.errorOccurred.connect(errors.append)
        worker.emitter.finished.connect(finished.append)
        worker.run()
        assert errors == [] and finished == [True]
        actual_x, actual_y = worker.axis_data["x"], worker.axis_data["y"]
        assert worker.axis_data["x" if mixed_column == "x" else "y"].dtype == object
        if reverse:
            actual_x, actual_y, expected = actual_x[::-1], actual_y[::-1], expected[::-1]
        operated = differentiate("x", {"x": actual_x, "y": actual_y, "z": None})["y"]
        np.testing.assert_array_equal(operated, expected)
        line = NativePlotDataItem(x=actual_x, y=actual_y, derivativeMode=True)
        np.testing.assert_array_equal(line.getData()[1], expected[[0, 2]])
    finally:
        dataset.conn.close()
    assert _source_state(path) == before


@pytest.mark.parametrize("reverse", [False, True])
def test_native_qt_derivative_after_exact_decimal_clipping(tmp_path, reverse):
    x, y = np.array([-1e308, 0., 1e308]), np.array([-1.7e308, 1.7e308, 0.])
    if reverse:
        x, y = x[::-1], y[::-1]
    expected_y = np.array([min(_fraction(value), Fraction(Decimal("1.6e308")))
                           for value in y], dtype=object)
    expected = _derivative_oracle(x, expected_y)[[0, 2]]
    path, run_id = _measurement(tmp_path, x, y, mixed=True)
    before = _source_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        params = {p.name: p for p in dataset.get_parameters()}
        operation = OperationCall("Limit Maximum", partial(pass_filter, "low", Decimal("1.6e308")),
                                  cooperative=True)
        worker = loader(dataset.cache, params["signal"], params, {"x": "x"}, operations=[operation])
        errors, finished = [], []
        worker.emitter.errorOccurred.connect(errors.append)
        worker.emitter.finished.connect(finished.append)
        worker.run()
        assert errors == [] and finished == [True]
        original_x, original_y = worker.axis_data["x"].copy(), worker.axis_data["y"].copy()
        line = NativePlotDataItem(x=original_x, y=original_y, derivativeMode=True)
        np.testing.assert_array_equal(line.getData()[1], expected)
        np.testing.assert_array_equal(line.getOriginalDataset()[0], original_x)
        np.testing.assert_array_equal(line.getOriginalDataset()[1], original_y)
    finally:
        dataset.conn.close()
    assert _source_state(path) == before


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("missing", [np.nan, np.inf, -np.inf])
def test_extreme_object_derivative_keeps_missing_stencil_semantics(axis, missing):
    x = np.array([-1e308, 0., 1e308])
    z = np.array([[-1e308, missing, 1e308]], dtype=object)
    data = {"x": x, "y": np.array([0.]), "z": z}
    if axis == "y":
        data = {"x": np.array([0.]), "y": x, "z": z.T}
    original = data["z"].copy()
    actual = differentiate(axis, data)["z"]
    # Uniform interiors use the outer samples, even when the centre is absent.
    expected = np.array([[np.nan, 1., np.nan]] if np.isnan(missing)
                        else [[missing, 1., -missing]])
    if axis == "y":
        expected = expected.T
    np.testing.assert_array_equal(actual, expected)
    assert data["z"].dtype == original.dtype
    assert all(value is original_value
               for value, original_value in zip(data["z"].flat, original.flat, strict=True))


@pytest.mark.parametrize("case", ["weight-underflow", "mixed-edge", "mixed-centre", "clipped"])
def test_exact_numeric_derivative_repairs_remain_cancellable(case):
    x = np.array([0., 1e-200, 1e200])
    row = np.array([0., 0., 1e308])
    if case != "weight-underflow":
        x = np.array([-1e308, 0., 1e308])
        row = np.array([-1e308, 1e308, 0.] if case == "mixed-edge"
                       else [-1e308, 0., 1e308], dtype=object)
    if case == "clipped":
        row = np.array([-1.7e308, Decimal("1.6e308"), 0], dtype=object)
    z = np.tile(row, (4096, 1))
    original = z.copy()
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 4

    with pytest.raises(InterruptedError, match="cancelled"):
        differentiate("x", {"x": x, "y": np.arange(len(z)), "z": z},
                      cancelled_callback=cancelled)
    np.testing.assert_array_equal(z, original)
