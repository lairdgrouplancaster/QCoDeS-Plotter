"""Numeric fidelity through the real Export Plot dialog and QCoDeS data."""

import csv
from decimal import Decimal
from fractions import Fraction

import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw

from qplot.tools import plot_csv as plot_csv_module
from qplot.windows import _plot_export as plot_export_module
from tests.support.windows.plot_csv_precision import (
    _create_measurements as _create_measurements,
)
from tests.support.windows.plot_csv_precision import (
    _export_from_dialog as _export_from_dialog,
)
from tests.support.windows.plot_csv_precision import (
    _open_csv_dialog as _open_csv_dialog,
)
from tests.support.windows.plot_csv_precision import (
    precision_plot as precision_plot,
)
from tests.support.windows.plot_integration import (
    wait_for,
)

_CURRENTS = np.array([1e-12, 2e-12, 3e-12, 9.99e-12, 1.23e-10])
_TINY = np.array([
    1e-12, -2e-12, 9.99e-12, -1.23e-10,
    1e-100, -1e-100, 1e-300, -1e-300,
    np.nextafter(0.0, 1.0), np.nextafter(0.0, -1.0),
])
_CASES = {
    "reported_currents": (np.arange(5, dtype=float), _CURRENTS),
    "tiny_xy": (_TINY[::-1], _TINY),
    "large_offsets": (
        1e6 + np.arange(5) * np.spacing(1e6),
        -1e12 + np.arange(5) * np.spacing(1e12),
    ),
}


@pytest.mark.parametrize("value", [
    pytest.param(2**4096, id="huge-integer"),
    pytest.param(Fraction(2**4096, 3), id="huge-fraction"),
    pytest.param(Decimal("1e1000"), id="huge-decimal"),
])
@pytest.mark.parametrize("missing", [
    pytest.param(float("nan"), id="float-nan"),
    pytest.param(Decimal("NaN"), id="decimal-nan"),
    pytest.param(np.longdouble("nan"), id="extended-nan"),
])
def test_shared_csv_coordinates_match_exact_values_and_nan_holes(value, missing):
    reference = np.array([value, missing], dtype=object)
    candidate = np.array([value, float("nan")], dtype=object)
    assert plot_export_module._csv_shared_coordinates_match(reference, candidate)
    candidate[0] = value * 2 if isinstance(value, Decimal) else value + 1
    assert not plot_export_module._csv_shared_coordinates_match(reference, candidate)
    candidate[:] = [value, 0]
    assert not plot_export_module._csv_shared_coordinates_match(reference, candidate)


@pytest.mark.parametrize("suffix,delimiter", [("csv", ","), ("tsv", "\t")])
@pytest.mark.parametrize(
    "precision_plot", [[values] for values in _CASES.values()],
    ids=list(_CASES), indirect=True,
)
def test_default_dialog_round_trips_measurement_xy(
    precision_plot, tmp_path, monkeypatch, suffix, delimiter,
):
    plot, expected = precision_plot
    dialog = _open_csv_dialog(plot)
    # Leave all displayed export options at their defaults.
    assert dialog.currentExporter.params["columnMode"] == "(x,y) per plot"
    rows = _export_from_dialog(
        monkeypatch, plot, dialog, tmp_path / f"precise.{suffix}", delimiter,
    )
    assert len(rows[0]) == 2
    np.testing.assert_array_equal(np.asarray(rows[1:], dtype=float), expected[0])
    assert "precision" not in [child.name() for child in dialog.currentExporter.params.children()]


@pytest.mark.parametrize("suffix,delimiter", [("csv", ","), ("tsv", "\t")])
@pytest.mark.parametrize("axes", [("Bottom", "Right"), ("Top", "Left"), ("Top", "Right")])
@pytest.mark.parametrize(
    "precision_plot",
    [[(_TINY[::-1], _TINY), (_TINY[::-1][:5], -_CURRENTS)]],
    indirect=True,
)
def test_dialog_round_trips_merged_secondary_traces_and_column_modes(
    precision_plot, tmp_path, monkeypatch, suffix, delimiter, axes,
):
    plot, expected = precision_plot
    for key, line in plot.lines.items():
        style = plot._trace_styles[key]
        style.x_axis, style.y_axis = axes
        plot._apply_trace_style(key, line)
    dialog = _open_csv_dialog(plot)
    # Reuse the actual dialog/exporter to check repeated exports as well as
    # both column layouts and padding for traces with different lengths.
    for index, mode in enumerate(("(x,y) per plot", "(x,y,y,y) for all plots")):
        dialog.currentExporter.params["columnMode"] = mode
        rows = _export_from_dialog(
            monkeypatch, plot, dialog, tmp_path / f"merged-{index}.{suffix}", delimiter,
        )
        columns = 4 if index == 0 else 3
        assert len(rows[0]) == columns
        assert len(rows) == len(expected[0]) + 1
        np.testing.assert_array_equal(
            np.asarray([row[:2] for row in rows[1:]], dtype=float), expected[0],
        )
        secondary_columns = [row[2:] for row in rows[1:len(expected[1]) + 1]]
        np.testing.assert_array_equal(
            np.asarray(secondary_columns, dtype=float),
            expected[1] if index == 0 else expected[1][:, 1:],
        )
        assert all(row[2:] == [""] * (columns - 2) for row in rows[len(expected[1]) + 1:])


def _assert_reused_dialog_rows(rows, expected, mode, names):
    shared_x = mode == "(x,y,y,y) for all plots"
    pairs = [
        [f"{name}_x", f"{name}_y"] if name is not None
        else [f"x{index:04}", f"y{index:04}"]
        for index, name in enumerate(names)
    ]
    assert rows[0] == pairs[0] + (pairs[1][1:] if shared_x else pairs[1])
    assert len(rows) == len(expected[0]) + 1
    assert all(len(row) == len(rows[0]) for row in rows)
    np.testing.assert_array_equal(
        np.asarray([row[:2] for row in rows[1:]], dtype=float), expected[0],
    )
    np.testing.assert_array_equal(
        np.asarray([row[2:] for row in rows[1:len(expected[1]) + 1]], dtype=float),
        expected[1][:, 1:] if shared_x else expected[1],
    )
    assert all(
        row[2:] == [""] * (len(rows[0]) - 2)
        for row in rows[len(expected[1]) + 1:]
    )


@pytest.mark.parametrize("suffix,delimiter", [("csv", ","), ("tsv", "\t")])
@pytest.mark.parametrize("named", [False, True], ids=["indexed_headers", "named_headers"])
@pytest.mark.parametrize("initial_mode", ["(x,y,y,y) for all plots", "(x,y) per plot"])
@pytest.mark.parametrize(
    "precision_plot",
    [[(_TINY[::-1], _TINY), (_TINY[::-1][:5], -_CURRENTS)]],
    indirect=True,
)
def test_successive_dialog_clicks_keep_shared_x_and_selected_options(
    precision_plot, tmp_path, monkeypatch, suffix, delimiter, named, initial_mode,
):
    plot, expected = precision_plot
    lines = list(plot.lines.values())
    if not named:
        for line in lines:
            line.opts["name"] = None
    names = [line.name() for line in lines]
    dialog = _open_csv_dialog(plot)
    exporter = dialog.currentExporter
    # A conflicting filename suffix still determines the file's delimiter,
    # without rewriting the option selected in the reused dialog.
    exporter.params["separator"] = "tab" if suffix == "csv" else "comma"
    shared_rows = None
    for index, mode in enumerate((initial_mode, "(x,y,y,y) for all plots", "(x,y,y,y) for all plots")):
        exporter.params["columnMode"] = mode
        selected_options = exporter.params.saveState()
        rows = _export_from_dialog(
            monkeypatch, plot, dialog, tmp_path / f"repeated-{index}.{suffix}", delimiter,
        )
        _assert_reused_dialog_rows(rows, expected, mode, names)
        assert dialog.currentExporter is exporter
        assert exporter.params.saveState() == selected_options
        if mode == "(x,y,y,y) for all plots":
            if shared_rows is not None:
                assert rows == shared_rows
            shared_rows = rows


@pytest.mark.parametrize("suffix,delimiter", [("csv", ","), ("tsv", "\t")])
@pytest.mark.parametrize(
    "precision_plot",
    [[(_TINY[::-1], _TINY), (_TINY[::-1][:5], -_CURRENTS)]],
    indirect=True,
)
def test_dialog_retry_after_partial_csv_write_has_fresh_state(
    precision_plot, tmp_path, monkeypatch, suffix, delimiter,
):
    plot, expected = precision_plot
    for line in plot.lines.values():
        line.opts["name"] = None
    dialog = _open_csv_dialog(plot)
    exporter = dialog.currentExporter
    exporter.params["columnMode"] = "(x,y,y,y) for all plots"
    exporter.params["separator"] = "tab" if suffix == "tsv" else "comma"
    selected_options = exporter.params.saveState()
    target = tmp_path / f"retry.{suffix}"
    original = b"previous export must survive a failed replacement\n"
    target.write_bytes(original)
    entries_before = set(tmp_path.iterdir())
    errors = []
    statuses = []
    prompts = []
    answers = iter((qtw.QMessageBox.StandardButton.No,
                    qtw.QMessageBox.StandardButton.Yes,
                    qtw.QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(plot, "show_error", lambda *args: errors.append(args))
    monkeypatch.setattr(plot, "show_status", lambda *args: statuses.append(args))
    monkeypatch.setattr(
        qtw.QFileDialog, "getSaveFileName",
        lambda *_args, **_kwargs: (str(target), ""),
    )

    def approve_replacement(_parent, _title, message, *_args):
        assert str(target) in message
        assert target.read_bytes() == original
        prompts.append(message)
        return next(answers)

    monkeypatch.setattr(qtw.QMessageBox, "question", approve_replacement)
    native_writer = csv.writer
    written_rows = []

    def partial_writer(stream, **kwargs):
        writer = native_writer(stream, **kwargs)

        class FailAfterFirstDataRow:
            def writerow(self, row):
                writer.writerow(row)
                written_rows.append(row)
                if len(written_rows) == 2:
                    raise OSError("Simulated CSV failure after header and first data row")

        return FailAfterFirstDataRow()

    with monkeypatch.context() as failure:
        failure.setattr(plot_csv_module.csv, "writer", partial_writer)
        dialog.ui.exportBtn.click()  # Declined replacement does not serialize.
        assert written_rows == []
        assert errors == []
        dialog.ui.exportBtn.click()  # Approved replacement fails midway.
        task, = plot._export_tasks
        wait_for(lambda: task.done)
    assert len(written_rows) == 2
    assert isinstance(task.error, OSError)
    assert "Simulated CSV failure" in statuses[-1][0]
    assert target.read_bytes() == original
    assert set(tmp_path.iterdir()) == entries_before
    assert exporter.params.saveState() == selected_options

    dialog.ui.exportBtn.click()  # Retry in the same dialog and exporter.
    task, = plot._export_tasks
    wait_for(lambda: task.done)
    assert task.error is None and not task.cancelled
    assert len(prompts) == 3
    assert errors == []
    assert statuses[-1][0] == f"Exported plot: {target}"
    assert dialog.currentExporter is exporter
    assert exporter.params.saveState() == selected_options
    assert set(tmp_path.iterdir()) == entries_before
    with target.open(newline="", encoding="utf-8") as exported:
        rows = list(csv.reader(exported, delimiter=delimiter))
    _assert_reused_dialog_rows(rows, expected, "(x,y,y,y) for all plots", [None, None])
