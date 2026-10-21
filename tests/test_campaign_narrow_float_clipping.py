"""Finite clipping bounds must not overflow or vanish in narrow QCoDeS arrays."""

import csv
from decimal import Decimal
from functools import partial

import numpy as np
import pytest

from qplot.tools.operation_registry import OperationCall
from qplot.tools.plot_tools import differentiate, pass_filter
from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker, run_worker
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_numeric_sample_preservation import array_plot
from tests.windows.test_plot_csv_precision import _export_from_dialog, _open_csv_dialog

_BOUNDS = [
    (np.float16, "Limit Minimum", "70000"),
    (np.float16, "Limit Maximum", "1e-8"),
    (np.float32, "Limit Minimum", "1e40"),
    (np.float32, "Limit Maximum", "1e-46"),
]


@pytest.mark.parametrize("dtype,operation,bound", _BOUNDS)
@pytest.mark.parametrize("heatmap", [False, True])
def test_real_narrow_float_clipping_controls_plot_and_export(
    tmp_path, monkeypatch, dtype, operation, bound, heatmap,
):
    signal = np.array([1, 2, 3, 4], dtype=dtype)
    records = [dict(x=np.arange(4), signal=signal.copy())]
    if heatmap:
        records = [dict(x=np.arange(4), slow=np.full(4, row), signal=signal.copy())
                   for row in range(2)]
    with array_plot(tmp_path, monkeypatch, records, heatmap=heatmap) as (window, plot):
        original = plot.dataGrid.copy() if heatmap else plot.axis_data["y"].copy()
        option = operation_option(plot, operation)
        option.input.setChecked(True)
        option.operation_row.input.setText(bound)
        _worker, finished, errors = apply_operations(plot)
        assert finished == [True]
        assert errors == []
        actual = plot.dataGrid if heatmap else plot.line.getOriginalDataset()[1]
        expected = np.full(original.shape, float(bound))
        np.testing.assert_array_equal(actual, expected)
        assert np.isfinite(actual).all()
        if heatmap:
            assert not plot._heatmap_color_range_rejected
        else:
            np.testing.assert_array_equal(plot.line.getData()[1], expected)

        dialog = _open_csv_dialog(plot)
        rows = _export_from_dialog(monkeypatch, plot, dialog, tmp_path / "clipped.csv", ",")
        np.testing.assert_array_equal(
            [float(row[2 if heatmap else 1]) for row in rows[1:]], expected.ravel(),
        )

        # Export Run must retain the acquired data despite the displayed operation.
        destination = tmp_path / "raw.csv"
        monkeypatch.setattr(window, "_choose_csv_export_filename", lambda _default: str(destination))
        window._export_measurement_csv(plot._dataset_key, ["signal"])
        with destination.open(newline="", encoding="utf-8") as stream:
            raw = [float(row["signal"]) for row in csv.DictReader(stream)]
        np.testing.assert_array_equal(raw, original.ravel())


@pytest.mark.parametrize("dtype,bound", [(np.float16, "1e-8"), (np.float32, "1e-46")])
@pytest.mark.parametrize("which", ["low", "high"])
def test_narrow_float_clipping_then_derivative_preserves_finite_residuals(
    tmp_path, dtype, bound, which,
):
    # The exact expected secants retain a nonzero clipping bound. A cast to the
    # acquisition dtype erases it before the derivative can recover its scale.
    sign = 1 if which == "low" else -1
    limit = sign * Decimal(bound)
    step = float(bound)
    coordinates = np.array([0., step, 2 * step])
    signal = np.array([0, sign, 2 * sign], dtype=dtype)
    operations = [
        OperationCall("Clip", partial(pass_filter, which, limit), cooperative=True),
        OperationCall("dz/dx", partial(differentiate, "x"),
                      derivative_axis="x", cooperative=True),
    ]
    records = [(row, coordinates, signal) for row in range(2)]
    with heatmap_dataset(tmp_path, records=records, planned_shape=(2, 3)) as dataset:
        raw = make_worker(dataset)
        run_worker(raw)
        processed = make_worker(dataset, operations=operations)
        run_worker(processed)
        np.testing.assert_array_equal(processed.dataGrid, np.tile([sign, sign * .5, 0.], (2, 1)))
        np.testing.assert_array_equal(raw.dataGrid, np.tile(signal, (2, 1)))


@pytest.mark.parametrize("dtype,bound", [(np.float16, "70000"), (np.float32, "1e40")])
@pytest.mark.parametrize("which", ["low", "high"])
def test_narrow_clipping_retains_unchanged_samples_nan_and_infinity(dtype, bound, which):
    sign = 1 if which == "high" else -1
    values = np.array([-np.inf, -0., .1, np.inf, np.nan], dtype=dtype)
    original = values.copy()
    actual = pass_filter(which, sign * Decimal(bound), {"y": values, "z": None})["y"]
    expected = np.array(
        [float(bound), float(bound), float(bound), np.inf, np.nan]
        if which == "high" else [-np.inf, -float(bound), -float(bound), -float(bound), np.nan],
    )
    np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(values, original)
    assert np.signbit(values[1])
    finite = values[1:3]
    no_op = pass_filter("high" if which == "low" else "low", sign * Decimal(bound),
                        {"y": finite, "z": None})["y"]
    np.testing.assert_array_equal(no_op, finite)
    assert np.signbit(no_op[0])
