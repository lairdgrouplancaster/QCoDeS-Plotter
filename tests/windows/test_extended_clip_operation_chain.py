"""Configured clipping retains the precision used by recorded float arrays."""

from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from qcodes.dataset.measurements import DataSaver

from qplot.tools.plot_tools import pass_filter
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors,
)
from tests.windows.test_numeric_sample_preservation import array_plot
from tests.windows.test_plot_csv_precision import _export_from_dialog, _open_csv_dialog

no_callback_errors = _no_callback_errors

_EXTENDED = np.finfo(np.longdouble).nmant > np.finfo(float).nmant
_DTYPES = [
    np.float64,
    pytest.param(np.longdouble, marks=pytest.mark.skipif(
        not _EXTENDED, reason="Platform has no extended floating precision",
    )),
]


def _exact_decimal(value):
    numerator, denominator = value.as_integer_ratio()
    with localcontext() as context:
        context.prec = 100
        return Decimal(numerator) / Decimal(denominator)


@pytest.mark.parametrize("dtype", _DTYPES)
@pytest.mark.parametrize("which", ["Maximum", "Minimum"])
@pytest.mark.parametrize("surface", ["line", "rows", "columns"])
def test_recorded_clipping_then_derivative_controls_plot_export_and_reset(
        tmp_path, monkeypatch, no_callback_errors, dtype, which, surface):
    destination = (tmp_path / "exact_arrays.db").resolve()
    add_result = DataSaver.add_result

    def add_to_owned_database(saver, *args, **kwargs):
        assert Path(saver.dataset.path_to_db).resolve() == destination
        return add_result(saver, *args, **kwargs)

    monkeypatch.setattr(DataSaver, "add_result", add_to_owned_database)
    signal = dtype(1) + np.array([0, 2, 4], dtype=dtype) * np.finfo(dtype).eps
    bound = _exact_decimal(signal[1])
    samples = [Fraction(*value.as_integer_ratio()) for value in signal]
    select = min if which == "Maximum" else max
    clipped = [select(value, Fraction(bound)) for value in samples]
    expected = np.array([
        float(clipped[1] - clipped[0]),
        float((clipped[2] - clipped[0]) / 2),
        float(clipped[2] - clipped[1]),
    ])
    if surface == "line":
        record = {"x": np.arange(3.), "signal": signal}
        derivative = "dy/dx"
        original = signal
    else:
        original = np.tile(signal, (2, 1))
        if surface == "columns":
            original = original.T
        height, width = original.shape
        record = {
            "x": np.tile(np.arange(width), (height, 1)),
            "slow": np.repeat(np.arange(height)[:, None], width, axis=1),
            "signal": original,
        }
        expected = np.tile(expected, (2, 1))
        if surface == "columns":
            expected = expected.T
        derivative = "dz/dx" if surface == "rows" else "dz/dy"

    with array_plot(
        tmp_path, monkeypatch, [record],
        shape=original.shape, heatmap=surface != "line",
    ) as (_window, plot):
        published_before = (plot.axis_data["y"] if surface == "line" else plot.dataGrid).copy()
        # An unshaped raw heatmap may publish Decimal cells after its SQL
        # aggregation. Check their recorded precision without Decimal's
        # unsupported direct comparison with NumPy extended floats.
        round_trip = np.array([str(value) for value in published_before.flat], dtype=dtype)
        np.testing.assert_array_equal(round_trip.reshape(original.shape), original)
        option = operation_option(plot, "Limit " + which)
        option.input.setChecked(True)
        option.operation_row.input.setText(str(bound))
        assert option.operation_row.input.hasAcceptableInput()
        assert option.operation_row.output() == bound
        operation_option(plot, derivative).input.setChecked(True)
        assert apply_operations(plot)[1:] == ([True], [])
        actual = plot.axis_data["y"] if surface == "line" else plot.dataGrid
        np.testing.assert_array_equal(actual, expected)
        if surface == "line":
            np.testing.assert_array_equal(plot.line.getData()[1], expected)
            np.testing.assert_array_equal(plot.line.curve.getData()[1], expected)
        else:
            assert plot.image.isVisible()
            np.testing.assert_array_equal(plot.image.image, expected)

        dialog = _open_csv_dialog(plot)
        exported = _export_from_dialog(monkeypatch, plot, dialog, tmp_path / "clipped.csv", ",")
        np.testing.assert_array_equal([float(row[-1]) for row in exported[1:]], expected.ravel())

        plot.oper_widget.hide_all()
        assert apply_operations(plot)[1:] == ([True], [])
        restored = plot.axis_data["y"] if surface == "line" else plot.dataGrid
        np.testing.assert_array_equal(restored, published_before)


@pytest.mark.skipif(not _EXTENDED, reason="Platform has no extended floating precision")
@pytest.mark.parametrize("which", ["low", "high"])
@pytest.mark.parametrize("bound", [0.1, Decimal("0.1")])
def test_extended_clipping_keeps_float_or_decimal_bound_semantics(which, bound):
    values = np.array(["0.09", "0.1", "0.11", "nan"], dtype=np.longdouble)
    original = values.copy()
    # A direct binary64 argument means that exact binary number. Configured
    # Decimal text is instead converted directly to the recorded float dtype.
    threshold = np.longdouble(str(bound)) if isinstance(bound, Decimal) else np.longdouble(bound)
    select = min if which == "low" else max
    expected = np.array([select(value, threshold) for value in values[:3]], dtype=np.longdouble)
    result = pass_filter(which, bound, {"y": values, "z": None})["y"]
    np.testing.assert_array_equal(result[:3], expected)
    assert np.isnan(result[3])
    np.testing.assert_array_equal(values, original)
