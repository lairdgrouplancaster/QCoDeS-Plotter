"""Shared-coordinate CSV refuses traces acquired on different coordinates."""
import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw

from tests.windows.test_plot_csv_precision import _open_csv_dialog
from tests.windows.test_plot_csv_precision import (
    precision_plot as _precision_plot_fixture,
)

precision_plot = _precision_plot_fixture


@pytest.mark.parametrize("precision_plot", [
    [
        (np.arange(3.), np.array([3., 4., 5.])),
        (np.arange(3.) + 10, np.array([13., 14., 15.])),
    ],
    [
        (np.arange(2.), np.array([3., 4.])),
        (np.arange(3.), np.array([13., 14., 15.])),
    ],
    [
        (np.array([2**53 + 1, 2**53 + 9], dtype=np.uint64), np.array([3, 4], dtype=np.uint64)),
        (np.array([2**53, 2**53 + 8], dtype=float), np.array([13., 14.])),
    ],
], ids=["different_coordinates", "secondary_extends_reference", "integer_and_rounded_float"], indirect=True)
def test_shared_coordinates_refuse_mismatched_traces_and_allow_retry(precision_plot, tmp_path, monkeypatch):
    plot, expected = precision_plot
    dialog = _open_csv_dialog(plot)
    dialog.currentExporter.params["columnMode"] = "(x,y,y,y) for all plots"
    target = tmp_path / "shared.csv"
    errors = []
    monkeypatch.setattr(plot, "show_error", lambda *args: errors.append(args))
    monkeypatch.setattr(qtw.QFileDialog, "getSaveFileName", lambda *_a, **_kw: (str(target), "CSV files (*.csv)"))
    dialog.ui.exportBtn.click()
    assert not target.exists(), "Shared-X CSV must not relabel a trace against another trace's coordinates"
    assert errors
    assert "(x,y) per plot" in str(errors[-1])
    assert not list(target.parent.glob(f".{target.name}.*"))

    errors.clear()
    dialog.currentExporter.params["columnMode"] = "(x,y) per plot"
    dialog.ui.exportBtn.click()
    assert not errors
    import csv
    from decimal import Decimal

    with target.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.reader(stream))[1:]
    for trace_index, values in enumerate(expected):
        for row_index, (x, y) in enumerate(values):
            actual = rows[row_index][2 * trace_index:2 * trace_index + 2]
            for text, value in zip(actual, (x, y), strict=True):
                if isinstance(value, np.integer):
                    assert Decimal(text) == int(value)
                else:
                    assert float(text) == float(value)
