"""Exact integer heatmaps retain missing samples through real plot controls."""

from decimal import Decimal

import numpy as np
import pytest

from qplot.tools.plot_tools import differentiate, pass_filter
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_numeric_sample_preservation import array_plot
from tests.windows.test_plot_integration import wait_for


@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("operation,bound", [
    ("Limit Maximum", "1e20"),
    ("Limit Minimum", "0"),
    ("Limit Maximum", f"{2**63 + 2}.5"),
    ("Limit Minimum", f"{2**63 + 2}.5"),
])
def test_integer_heatmap_limits_preserve_holes(tmp_path, monkeypatch, shaped, operation, bound):
    base = 2**63
    records = [
        {"x": np.array([0., 1., 2.]), "slow": np.zeros(3),
         "signal": np.array([base + n for n in (1, 2, 3)], dtype=np.uint64)},
        {"x": np.array([0., np.nan, 2.]), "slow": np.ones(3),
         "signal": np.array([base + n for n in (4, 5, 6)], dtype=np.uint64)},
    ]
    with array_plot(tmp_path, monkeypatch, records, heatmap=True,
                    shape=(2, 3) if shaped else None) as (_, plot):
        original = plot.dataGrid.copy()
        option = operation_option(plot, operation)
        option.input.setChecked(True)
        option.operation_row.input.setText(bound)
        assert apply_operations(plot)[1:] == ([True], [])
        limit = Decimal(bound)
        expected_limit = min if operation == "Limit Maximum" else max
        for position in np.ndindex(original.shape):
            if position == (1, 1):
                assert np.isnan(plot.dataGrid[position])
            else:
                assert plot.dataGrid[position] == expected_limit(int(original[position]), limit)
        option.input.setChecked(False)
        assert apply_operations(plot)[1:] == ([True], [])
        np.testing.assert_array_equal(plot.dataGrid[0], original[0])
        assert np.isnan(plot.dataGrid[1, 1])


def _duplicate_records():
    base = 2**63 + 11
    return [
        {"x": np.array([0., 0., 1., 1., 2., 2., 3., 3.]), "slow": np.zeros(8),
         "signal": np.array([base + n for n in range(8)], dtype=np.uint64)},
        {"x": np.array([0., 0., np.nan, np.nan, 2., 2., 3., 3.]), "slow": np.ones(8),
         "signal": np.array([base + n for n in range(8)], dtype=np.uint64)},
    ]


def test_duplicate_integer_heatmap_gradient_preserves_finite_stencils(tmp_path, monkeypatch):
    with array_plot(tmp_path, monkeypatch, _duplicate_records(), heatmap=True) as (_, plot):
        operation_option(plot, "dz/dx").input.setChecked(True)
        assert apply_operations(plot)[1:] == ([True], [])
        np.testing.assert_array_equal(plot.dataGrid[0], [2., 2., 2., 2.])
        np.testing.assert_array_equal(plot.dataGrid[1], [np.nan, 2., np.nan, 2.])


@pytest.mark.parametrize("control", ["derivativeCheck", "phasemapCheck"])
def test_duplicate_integer_cut_native_difference_preserves_holes(tmp_path, monkeypatch, control):
    with array_plot(tmp_path, monkeypatch, _duplicate_records(), heatmap=True) as (window, plot):
        plot.z_index = [0, 1]
        plot.openSweep("h")
        cut = window.windows[-1]
        wait_for(lambda: not cut.worker.running)
        cut.monitor.stop()
        raw = cut.line.getOriginalDataset()[1].copy()
        assert isinstance(raw[0], Decimal)
        assert np.isnan(raw[1])
        click_control(cut, control)
        np.testing.assert_array_equal(cut.line.getData()[1], [np.nan, np.nan, 2.])
        click_control(cut, control)
        assert cut.line.getOriginalDataset()[1][0] == raw[0]
        assert np.isnan(cut.line.getOriginalDataset()[1][1])


@pytest.mark.parametrize("which", ["low", "high"])
def test_limit_entirely_missing_object_data(which):
    values = np.array([np.nan, Decimal("NaN")], dtype=object)
    result = pass_filter(which, Decimal("1.5"), {"y": values, "z": None})["y"]
    assert np.isnan(result[0])
    assert result[1].is_nan()


def test_decimal_difference_keeps_infinity_and_exact_finite_steps():
    values = np.array([np.inf, Decimal(2**63) + Decimal(".5"),
                       Decimal(2**63) + Decimal("1.5"), float("nan")], dtype=object)
    result = differentiate("x", {"x": np.arange(4), "y": values, "z": None})["y"]
    assert result[0] == -np.inf
    assert np.isnan(result[-1])


@pytest.mark.parametrize("axis", ["x", "y"])
def test_integer_uniform_gradient_ignores_missing_central_sample(axis):
    base = 2**63
    values = np.array([[base, np.nan, base + 2, base + 3]], dtype=object)
    if axis == "y":
        values = values.T
    result = differentiate(axis, {"x": np.arange(values.shape[1]),
                                  "y": np.arange(values.shape[0]), "z": values})["z"]
    np.testing.assert_array_equal(result.ravel(), [np.nan, 1., np.nan, 1.])
