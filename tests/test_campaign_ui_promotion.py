"""Operations must keep their measured quantities when promoting overlays."""

import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw
from pyqtgraph.exporters import CSVExporter

from qplot.windows._plot_source_promotion import promote_plot_source
from tests.windows.test_mixed_plot_drops import (
    _added,
    _drop,
    _wait_for,
    mixed_plot,  # noqa: F401
)


def _process_source(source, operation, value=None):
    option = next(source.oper_widget.list_options.item(index)
                  for index in range(source.oper_widget.list_options.count())
                  if source.oper_widget.list_options.item(index).label == operation)
    option.input.setChecked(True)
    if value is not None:
        option.operation_row.input.setText(value)
    source.refreshWindow(force=True)
    _wait_for(lambda: not source.worker.running)


def _primary_entry(target):
    return next(entry for entry in target._plot_item_order
                if entry[1] == (target.label if target.operation_kind == "plot1d"
                                else target._primary_heatmap_key))


@pytest.mark.parametrize("operation,value", [("Limit Maximum", "1"), ("dy/dx", None), (None, None)])
def test_transposed_line_promotion_keeps_operation_semantics(mixed_plot, operation, value):  # noqa: F811
    window, target, runs = mixed_plot
    _drop(target, runs["other_line"])
    overlay = _added(target, runs["other_line"])
    source = overlay.from_win
    if operation is not None:
        _process_source(source, operation, value)
    target.set_plot_axes_swapped(True)
    _wait_for(lambda: not target.worker.running)
    overlay.refresh(source_ready=True)
    expected = np.asarray(overlay.getData()).copy()
    original_entry = _primary_entry(target)
    if operation is not None:
        before_windows = list(window.windows)
        assert "Swap this plot's X/Y axes" in target.plot_item_removal_error([original_entry])
        with pytest.raises(ValueError, match="Swap this plot's X/Y axes"):
            promote_plot_source(window, target, [original_entry])
        assert target.remove_plot_items([original_entry]) is target
        assert window.windows == before_windows
        np.testing.assert_array_equal(overlay.getData(), expected)
        target.set_plot_axes_swapped(False)
        _wait_for(lambda: not target.worker.running)
        overlay.refresh(source_ready=True)
        expected = np.asarray(overlay.getData()).copy()
    replacement = target.remove_plot_items([original_entry])
    assert replacement is not target
    _wait_for(lambda: not replacement.worker.running and not replacement._refresh_pending)
    np.testing.assert_array_equal(replacement.line.getData(), expected)


@pytest.mark.parametrize("operation", ["Subtract Row Mean", "Subtract Column Mean", "dz/dx", "dz/dy", "mean_then_clip"])
def test_transposed_heatmap_promotion_remaps_operation_axes(mixed_plot, operation, monkeypatch, tmp_path):  # noqa: F811
    _window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    _drop(target, runs["other_heatmap"])
    overlay = _added(target, runs["other_heatmap"])
    source = overlay.from_win
    if operation == "mean_then_clip":
        _process_source(source, "Subtract Row Mean")
        _process_source(source, "Limit Maximum", "0")
    else:
        _process_source(source, operation)
    target.set_plot_axes_swapped(True)
    _wait_for(lambda: not target.worker.running)
    overlay.refresh(source_ready=True)
    assert overlay.source_axis_order() == ("y", "x")
    expected = overlay.data_grid.copy()
    replacement = target.remove_plot_items([_primary_entry(target)])
    _wait_for(lambda: not replacement.worker.running and not replacement._refresh_pending)
    np.testing.assert_array_equal(replacement.dataGrid, expected)
    assert replacement.display_param.unit == source.display_param.unit
    assert replacement.display_param.label == source.display_param.label
    replacement.refreshWindow(force=True)
    _wait_for(lambda: not replacement.worker.running and not replacement._refresh_pending)
    np.testing.assert_array_equal(replacement.dataGrid, expected)
    destination = tmp_path / "promoted.csv"
    replacement._write_heatmap_csv_stage(str(destination), CSVExporter(replacement.plot))
    exported = np.loadtxt(destination, skiprows=1, delimiter=",")
    np.testing.assert_array_equal(exported[:, 2].reshape(expected.shape), expected)
