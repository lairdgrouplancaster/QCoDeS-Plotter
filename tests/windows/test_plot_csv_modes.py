"""Explicit plot CSV semantics, frozen inputs and cancellable serialization."""

import csv
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw
from pyqtgraph.exporters import CSVExporter

from qplot.tools.plot_csv import CsvColumns, HeatmapCsv, TransformedTrace, frozen_array
from qplot.windows._native_transforms import NativePlotDataItem
from qplot.windows._plot_export import (
    CSV_HEATMAP_VALUES,
    CSV_TRANSFORMED_VALUES,
    CSV_UNDERLYING_VALUES,
    PlotExportMixin,
)
from tests.windows.test_plot_integration import wait_for
from tests.windows.test_svg_export_integration import (
    real_plot as real_plot,
)
from tests.windows.test_svg_export_integration import (
    select_exporter,
)


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.reader(stream))
    return rows[0], np.asarray(rows[1:], dtype=float)


@pytest.fixture
def line_capture(qapplication):
    widget = pg.PlotWidget()
    plot = widget.getPlotItem()
    line = NativePlotDataItem(name="test")
    plot.addItem(line)
    owner = SimpleNamespace(
        axis_param={
            "x": SimpleNamespace(name="time", label="Time", unit="s"),
            "y": SimpleNamespace(name="current", label="Current", unit="nA"),
        },
    )
    try:
        yield owner, plot, line
    finally:
        widget.close()
        widget.deleteLater()
        qapplication.processEvents()


def test_csv_dialog_names_values_and_scope(real_plot):
    plot, _database, _errors, dimensions = real_plot
    dialog = select_exporter(plot, CSVExporter)
    params = dialog.currentExporter.parameters()
    mode = params.child("values")
    assert mode is not None
    assert "CSV data" in [dialog.ui.formatList.item(i).text()
                          for i in range(dialog.ui.formatList.count())]
    assert "precision" not in params.names
    if dimensions == 2:
        assert mode.value() == CSV_HEATMAP_VALUES
        assert "primary heatmap" in dialog.csv_scope.text()
        assert "spatial averaging" in dialog.csv_scope.text()
        assert "columnMode" not in params.names
    else:
        assert mode.value() == CSV_UNDERLYING_VALUES
        assert "before native FFT" in dialog.csv_scope.text()
        mode.setValue(CSV_TRANSFORMED_VALUES)
        assert "physical coordinates" in dialog.csv_scope.text()
        assert "rendering downsampling" in dialog.csv_scope.text()


def test_plot_csv_worker_can_cancel_without_publishing(real_plot, tmp_path, monkeypatch):
    plot, _database, errors, dimensions = real_plot
    target = tmp_path / "cancelled.csv"
    target.write_bytes(b"existing destination")
    entered, release = threading.Event(), threading.Event()
    snapshot_type = HeatmapCsv if dimensions == 2 else CsvColumns
    original_write = snapshot_type.write

    def blocked_write(snapshot, *args, **kwargs):
        assert QtCore.QThread.currentThread() is not qtw.QApplication.instance().thread()
        entered.set()
        assert release.wait(5)
        return original_write(snapshot, *args, **kwargs)

    monkeypatch.setattr(snapshot_type, "write", blocked_write)
    monkeypatch.setattr(qtw.QFileDialog, "getSaveFileName", lambda *_args, **_kwargs: (str(target), ""))
    monkeypatch.setattr(qtw.QMessageBox, "question",
                        lambda *_args, **_kwargs: qtw.QMessageBox.StandardButton.Yes)
    task = None
    try:
        assert plot._export_pyqtgraph_exporter(CSVExporter(plot.plot))
        task, = plot._export_tasks
        wait_for(entered.is_set)
        assert not task.done
        assert target.read_bytes() == b"existing destination"
        task.cancel()
    finally:
        release.set()
        if task is not None:
            wait_for(lambda: task.done)
    assert task.cancelled
    assert target.read_bytes() == b"existing destination"
    assert not list(tmp_path.glob(".cancelled.csv.*"))
    assert not errors


@pytest.mark.parametrize("mode", ["fft", "subtract_mean", "derivative"])
def test_numeric_transform_checks_cancellation_during_exact_arithmetic(mode):
    x = np.arange(10000, dtype=np.int64)
    y = np.array([2**60+int(value) for value in x], dtype=object)
    if mode == "derivative":
        # Mixed exact cells require the cancellable Fraction secant path.
        y[5000] = float(y[5000])
    trace = TransformedTrace(x, y, ("x", "y"), **{mode: True})
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 5

    with pytest.raises(InterruptedError):
        trace.values(cancelled)
    assert checks == 5


@pytest.mark.parametrize("reverse", [False, True])
def test_fft_export_is_frozen_full_spectrum_in_physical_units(line_capture, tmp_path, reverse):
    owner, plot, line = line_capture
    x = np.arange(64, dtype=float) * .125
    y = 4 + np.sin(2*np.pi*np.arange(64)/8)
    if reverse:
        x, y = x[::-1], y[::-1]
    line.setData(x, y)
    line.setFftMode(True)
    line.setLogMode(True, True)
    line.setDownsampling(ds=4, method="peak")
    line.setClipToView(True)
    plot.setXRange(1, 2, padding=0)
    exporter = CSVExporter(plot)
    frozen = PlotExportMixin._capture_line_csv(owner, exporter, transformed=True)
    line.setData([0, 1], [99, 101])
    target = tmp_path / "fft.csv"
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(frozen.write, target).result(timeout=5)
    headers, result = read_csv(target)
    assert headers == ["test: Frequency of Time [1/s]", "test: FFT magnitude of Current [nA]"]
    assert result.shape == (33, 2)
    np.testing.assert_allclose(result[:, 0], np.fft.rfftfreq(64, .125))
    np.testing.assert_allclose(result[:, 1], np.abs(np.fft.rfft(y[::-1] if reverse else y))/64,
                               atol=1e-14)
    assert result[0, 0] == 0  # Log-axis rendering omits this, export retains it.


@pytest.mark.parametrize("mode", ["derivative", "phase", "mean"])
def test_processed_values_follow_native_controls_but_underlying_stays_exact(
    line_capture, tmp_path, mode,
):
    owner, plot, line = line_capture
    x = np.array([0, 1, 3, 6], dtype=np.int64)
    y = np.array([2**60, 2**60+2, 2**60+8, 2**60+20], dtype=np.int64)
    line.setData(x, y)
    if mode == "derivative":
        line.setDerivativeMode(True)
        expected = np.column_stack((x[:-1], [2., 3., 4.]))
    elif mode == "phase":
        line.setPhasemapMode(True)
        expected = np.column_stack((y[:-1], [2., 3., 4.]))
    else:
        line.setSubtractMeanMode(True)
        expected = np.column_stack((x, [-7.5, -5.5, .5, 12.5]))
    exporter = CSVExporter(plot)
    underlying = PlotExportMixin._capture_line_csv(owner, exporter)
    transformed = PlotExportMixin._capture_line_csv(owner, exporter, transformed=True)
    underlying.write(tmp_path / "underlying.csv")
    transformed.write(tmp_path / "transformed.csv")
    with (tmp_path / "underlying.csv").open(newline="") as stream:
        rows = list(csv.reader(stream))
    assert [int(row[1]) for row in rows[1:]] == y.tolist()
    _headers, result = read_csv(tmp_path / "transformed.csv")
    np.testing.assert_array_equal(result, expected)
    if mode == "phase":
        with (tmp_path / "transformed.csv").open(newline="") as stream:
            rows = list(csv.reader(stream))
        assert [int(row[0]) for row in rows[1:]] == y[:-1].tolist()


def test_shared_column_validation_uses_transformed_coordinates(line_capture, tmp_path):
    owner, plot, line = line_capture
    x = np.arange(16, dtype=float)
    line.setData(x, x*x)
    line.setFftMode(True)
    other = NativePlotDataItem(x=x, y=x, name="other")
    plot.addItem(other)
    exporter = CSVExporter(plot)
    exporter.params["columnMode"] = "(x,y,y,y) for all plots"
    frozen = PlotExportMixin._capture_line_csv(owner, exporter, transformed=True)
    with pytest.raises(ValueError, match="different X coordinates"):
        frozen.write(tmp_path / "mismatched.csv")
    assert not (tmp_path / "mismatched.csv").exists()


def test_heatmap_export_freezes_current_grid_without_a_database_read(tmp_path):
    x, y = np.array([1, 3]), np.array([2, 4, 6])
    grid = np.arange(6).reshape(3, 2)
    owner = SimpleNamespace(
        axis_data={"x": x, "y": y}, dataGrid=grid,
        _heatmap_csv_column_names=lambda: ("bias", "gate", "signal"),
    )
    frozen = PlotExportMixin._capture_heatmap_csv(owner)
    grid[:] = 99
    x[:] = -1
    frozen.write(tmp_path / "grid.csv")
    headers, result = read_csv(tmp_path / "grid.csv")
    assert headers == ["bias", "gate", "signal"]
    np.testing.assert_array_equal(result, [[1, 2, 0], [3, 2, 1], [1, 4, 2],
                                          [3, 4, 3], [1, 6, 4], [3, 6, 5]])


def test_csv_serialization_checks_cancellation_between_chunks(tmp_path):
    snapshot = CsvColumns(("x",), (frozen_array(np.arange(50_000)),))
    cancelled = False
    def progress(_phase, completed, _total):
        nonlocal cancelled
        if completed >= 1024:
            cancelled = True
    with pytest.raises(InterruptedError, match="cancelled"):
        snapshot.write(tmp_path / "stage.csv", lambda: cancelled, progress)
    with (tmp_path / "stage.csv").open() as stream:
        assert sum(1 for _ in stream) <= 2049
