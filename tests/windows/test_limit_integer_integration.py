"""Integer limits through stored QCoDeS arrays, Operations, and Plot CSV."""

import csv
from decimal import Decimal
from fractions import Fraction

import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw
from pyqtgraph.exporters import CSVExporter

from qplot.datahandling.qcodes_cache import cache_parameter_data
from qplot.tools.plot_tools import pass_filter
from qplot.windows._plot1d_snap import _line_snap_data
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_differentiation_integration import (
    integer_array_plot as _integer_array_plot_fixture,
)

integer_array_plot = _integer_array_plot_fixture


def _plot_csv_from_action(plot, monkeypatch, target):
    plot.exportPlotAction.trigger()
    qtw.QApplication.processEvents()
    dialog = plot.widget.scene().exportDialog
    assert dialog.isVisible()
    for row in range(dialog.ui.formatList.count()):
        if dialog.ui.formatList.item(row).expClass is CSVExporter:
            dialog.ui.formatList.setCurrentRow(row)
            break
    assert type(dialog.currentExporter) is CSVExporter
    assert dialog.currentExporter.item is plot.plot
    monkeypatch.setattr(
        qtw.QFileDialog, "getSaveFileName",
        lambda *_args, **_kwargs: (str(target), ""),
    )
    dialog.ui.exportBtn.click()
    assert target.exists()
    with target.open(newline="", encoding="utf-8") as stream:
        return list(csv.reader(stream))


_UINT = 2**63
_SIGNED_MIN, _SIGNED_MAX = -(2**63), 2**63 - 1
_CASES = [
    ("Limit Maximum", "1e20", np.array([_UINT + n for n in (3, 2, 1, 0)], dtype=np.uint64)),
    ("Limit Minimum", "-1e20", np.array([_SIGNED_MIN + n for n in range(4)])),
    ("Limit Maximum", "2", np.array([_SIGNED_MIN, _SIGNED_MIN + 1, 0, _SIGNED_MAX])),
    ("Limit Minimum", "2", np.array([0, 1, _UINT + 1, _UINT + 2], dtype=np.uint64)),
    ("Limit Maximum", "1.5", np.array([_SIGNED_MIN, _SIGNED_MIN + 1, 0, 3])),
    ("Limit Minimum", "1.5", np.array([0, 1, _UINT + 1, _UINT + 2], dtype=np.uint64)),
    ("Limit Maximum", "-1.5", np.array([_SIGNED_MIN, _SIGNED_MIN + 1, 0, 3])),
    ("Limit Minimum", "-1.5", np.array([_SIGNED_MIN, -2, 0, _SIGNED_MAX])),
    ("Limit Maximum", str(_UINT), np.array([_UINT, _UINT + 1, _UINT + 2, _UINT + 3], dtype=np.uint64)),
    ("Limit Minimum", "1e20", np.array([_UINT + n for n in range(4)], dtype=np.uint64)),
    ("Limit Maximum", f"{_UINT + 1}.5", np.array([_UINT + n for n in range(4)], dtype=np.uint64)),
    ("Limit Minimum", f"{_SIGNED_MIN + 1}.5", np.array([_SIGNED_MIN + n for n in range(4)])),
]


@pytest.mark.parametrize(
    "integer_array_plot,operation,bound",
    [((np.arange(4), values, True), operation, bound) for operation, bound, values in _CASES],
    indirect=["integer_array_plot"],
    ids=[
        "unsigned-noop-maximum", "signed-noop-minimum",
        "signed-partial-integral-maximum", "unsigned-partial-integral-minimum",
        "signed-partial-fractional-maximum", "unsigned-partial-fractional-minimum",
        "signed-negative-fractional-maximum", "signed-negative-fractional-minimum",
        "unsigned-partial-boundary-maximum", "unsigned-all-clipped-minimum",
        "unsigned-large-fractional-maximum", "signed-large-fractional-minimum",
    ],
)
def test_integer_limits_keep_exact_plot_and_csv_values(
    integer_array_plot, operation, bound, tmp_path, monkeypatch,
):
    plot = integer_array_plot
    original = plot.axis_data["y"].copy()
    cache = {
        name: values.copy()
        for name, values in cache_parameter_data(plot.ds.cache, "signal").items()
    }
    option = operation_option(plot, operation)
    option.input.setChecked(True)
    option.operation_row.input.setText(bound)
    worker, finished, errors = apply_operations(plot)
    assert finished == [True]
    assert errors == []

    limit = Decimal(bound)
    expected = [
        min(Decimal(int(value)), limit) if operation == "Limit Maximum"
        else max(Decimal(int(value)), limit)
        for value in original
    ]
    assert [Decimal(str(value)) for value in worker.axis_data["y"]] == expected
    assert [Decimal(str(value)) for value in plot.line.getOriginalDataset()[1]] == expected
    snap = _line_snap_data(plot.line)
    assert snap is not None
    assert [Decimal(str(value)) for value in snap.y_raw] == expected
    rows = _plot_csv_from_action(plot, monkeypatch, tmp_path / "limited.csv")
    assert len(rows) == 5
    assert [Decimal(row[1]) for row in rows[1:]] == expected
    for name, values in cache.items():
        np.testing.assert_array_equal(cache_parameter_data(plot.ds.cache, "signal")[name], values)
    option.input.setChecked(False)
    assert apply_operations(plot)[1:] == ([True], [])
    np.testing.assert_array_equal(plot.axis_data["y"], original)


@pytest.mark.parametrize(
    "integer_array_plot,first,bound,second,other_bound,exact_edge",
    [
        ((np.arange(4), np.array([_SIGNED_MIN, _SIGNED_MIN + 1, 0, 3]), True),
         "Limit Maximum", "1.5", "Limit Minimum", "-1e20", 0),
        ((np.arange(4), np.array([0, 1, _UINT + 1, _UINT + 2], dtype=np.uint64), True),
         "Limit Minimum", "1.5", "Limit Maximum", "1e20", -1),
    ],
    indirect=["integer_array_plot"],
    ids=["maximum-then-minimum", "minimum-then-maximum"],
)
def test_fractional_limit_composes_with_native_controls_gradient_and_csv(
    integer_array_plot, first, bound, second, other_bound, exact_edge,
    tmp_path, monkeypatch,
):
    plot = integer_array_plot
    for name, value in ((first, bound), (second, other_bound)):
        option = operation_option(plot, name)
        option.input.setChecked(True)
        option.operation_row.input.setText(value)
    assert apply_operations(plot)[1:] == ([True], [])

    raw = plot.line.getOriginalDataset()[1]
    assert raw.dtype == object
    expected = [Decimal(str(value)) for value in raw]
    assert [Decimal(row[1]) for row in _plot_csv_from_action(
        plot, monkeypatch, tmp_path / "composed.csv",
    )[1:]] == expected
    assert plot.line.getData()[1].dtype.kind == "f"

    exact = [Fraction(value) for value in raw]
    mean = sum(exact) / len(exact)
    centered = np.array([float(value - mean) for value in exact])
    plot.plot.ctrl.subtractMeanCheck.setChecked(True)
    np.testing.assert_array_equal(plot.line.getData()[1], centered)
    plot.plot.ctrl.fftCheck.setChecked(True)
    np.testing.assert_allclose(
        plot.line.getData()[1], np.abs(np.fft.rfft(centered) / len(centered)),
    )
    plot.plot.ctrl.fftCheck.setChecked(False)
    plot.plot.ctrl.subtractMeanCheck.setChecked(False)

    plot.plot.ctrl.derivativeCheck.setChecked(True)
    snap = _line_snap_data(plot.line)
    assert snap is not None
    assert float(snap.y_raw[exact_edge]) == 1.0
    plot.plot.ctrl.derivativeCheck.setChecked(False)
    plot.plot.ctrl.phasemapCheck.setChecked(True)
    assert float(plot.line.getData()[1][exact_edge]) == 1.0
    plot.plot.ctrl.phasemapCheck.setChecked(False)

    derivative = operation_option(plot, "dy/dx")
    derivative.input.setChecked(True)
    worker, finished, errors = apply_operations(plot)
    assert finished == [True]
    assert errors == []
    assert worker.axis_data["y"][exact_edge] == 1.0
    assert plot.axis_data["y"][exact_edge] == 1.0


def test_integer_limit_cancellation_does_not_change_input():
    original = np.array([_UINT + n for n in range(4)], dtype=np.uint64)
    data = {"x": np.arange(4), "y": original.copy(), "z": None}
    checks = iter((False, True))
    with pytest.raises(InterruptedError, match="cancelled"):
        pass_filter(
            "low", 1.5, data, cancelled_callback=lambda: next(checks),
        )
    np.testing.assert_array_equal(data["y"], original)
