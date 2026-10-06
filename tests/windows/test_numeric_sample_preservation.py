"""Stored QCoDeS samples remain exact through the real plot controls."""

from contextlib import contextmanager
from decimal import Decimal
from fractions import Fraction

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.windows import MainWindow
from tests._window_lifecycle import close_main_window
from tests.windows.test_complex_line_data import database_state
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_plot_csv_precision import _export_from_dialog, _open_csv_dialog
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


@contextmanager
def array_plot(tmp_path, monkeypatch, records, *, shape=None, heatmap=False):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "exact_arrays.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("exact arrays", "test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array")
    if heatmap:
        measurement.register_custom_parameter("slow", paramtype="array")
    measurement.register_custom_parameter(
        "signal", paramtype="array", setpoints=("slow", "x") if heatmap else ("x",),
    )
    if shape is not None:
        measurement.set_shapes({"signal": shape})
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            for record in records:
                saver.add_result(*record.items())
            guid = saver.dataset.guid
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
    protected = database_state(path)
    window = MainWindow()
    errors = []
    monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active)
        prior_plot_count = len(window.windows)
        window.openPlot(guid=guid, show=True)
        wait_for(lambda prior_plot_count=prior_plot_count: len(window.windows) > prior_plot_count)
        plot = window.windows[-1]
        monkeypatch.setattr(plot, "show_error", lambda *args: errors.append(args))
        wait_for(lambda: not plot.worker.running)
        plot.monitor.stop()
        assert errors == []
        yield window, plot
        assert errors == []
    finally:
        close_main_window(window)
        assert database_state(path) == protected


@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("ragged", [False, True])
def test_mixed_integer_records_preserve_raw_csv_and_operation_steps(tmp_path, monkeypatch, shaped, ragged):
    tail = [-1, -3, -5] if ragged else [-1, -3]
    expected = [2**63 + 1, 2**63 + 3, *tail]
    records = [
        {"x": np.arange(2), "signal": np.array(expected[:2], dtype=np.uint64)},
        {"x": np.arange(2, len(expected)), "signal": np.array(tail, dtype=np.int64)},
    ]
    with array_plot(tmp_path, monkeypatch, records, shape=(len(expected),) if shaped else None) as (_window, plot):
        for refresh in (False, True):
            if refresh:
                plot.refreshWindow(force=True)
                wait_for(lambda: not plot.worker.running)
                plot.monitor.stop()
            assert plot.line.getOriginalDataset()[1].tolist() == expected
        dialog = _open_csv_dialog(plot)
        rows = _export_from_dialog(monkeypatch, plot, dialog, tmp_path / "mixed.csv", ",")
        assert [int(row[1]) for row in rows[1:]] == expected
        operation_option(plot, "dy/dx").input.setChecked(True)
        apply_operations(plot)
        assert plot.line.getOriginalDataset()[1][0] == 2.


@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("missing", [False, True])
def test_integer_heatmap_operations_cuts_and_csv(tmp_path, monkeypatch, shaped, missing):
    expected = [[2**63 + 11 + 8 * row + column for column in range(4)] for row in range(2)]
    records = []
    for row in range(2):
        x = np.arange(4, dtype=float)
        if missing and row == 1:
            x[2] = np.nan
        records.append({"x": x, "slow": np.full(4, row), "signal": np.array(expected[row], dtype=np.uint64)})
    with array_plot(tmp_path, monkeypatch, records, shape=(2, 4) if shaped else None, heatmap=True) as (window, plot):
        for row in range(2):
            for column in range(4):
                if missing and (row, column) == (1, 2):
                    assert np.isnan(plot.dataGrid[row, column])
                else:
                    assert int(plot.dataGrid[row, column]) == expected[row][column]
        dialog = _open_csv_dialog(plot)
        rows = _export_from_dialog(monkeypatch, plot, dialog, tmp_path / "heat.csv", ",")
        assert [int(row[2]) for row in rows[1:5]] == expected[0]
        plot.z_index = [0, 0]
        plot.openSweep("h")
        cut = window.windows[-1]
        wait_for(lambda: not cut.worker.running)
        cut.monitor.stop()
        assert cut.line.getOriginalDataset()[1].tolist() == expected[0]
        operation_option(plot, "Subtract Row Mean").input.setChecked(True)
        apply_operations(plot)
        np.testing.assert_array_equal(plot.dataGrid[0], [-1.5, -.5, .5, 1.5])
        if missing:
            values = [expected[1][index] for index in (0, 1, 3)]
            mean = sum(map(Fraction, values)) / len(values)
            np.testing.assert_array_equal(plot.dataGrid[1, [0, 1, 3]], [float(Fraction(value) - mean) for value in values])
            assert np.isnan(plot.dataGrid[1, 2])
        operation_option(plot, "Subtract Row Mean").input.setChecked(False)
        operation_option(plot, "dz/dx").input.setChecked(True)
        apply_operations(plot)
        np.testing.assert_array_equal(plot.dataGrid[0], np.ones(4))


def test_duplicate_integer_heatmap_cells_keep_fractional_mean(tmp_path, monkeypatch):
    base = 2**63 + 11
    records = [
        {"x": np.array([0, 0, 1, 1]), "slow": np.full(4, row),
         "signal": np.array([base, base + 1, base + 2, base + 3], dtype=np.uint64)}
        for row in range(2)
    ]
    with array_plot(tmp_path, monkeypatch, records, heatmap=True) as (_window, plot):
        assert plot.dataGrid[0].tolist() == [Decimal(base) + Decimal("0.5"), Decimal(base) + Decimal("2.5")]
        dialog = _open_csv_dialog(plot)
        rows = _export_from_dialog(monkeypatch, plot, dialog, tmp_path / "duplicate.csv", ",")
        assert [Decimal(row[2]) for row in rows[1:3]] == plot.dataGrid[0].tolist()
        operation_option(plot, "Subtract Row Mean").input.setChecked(True)
        apply_operations(plot)
        np.testing.assert_array_equal(plot.dataGrid, [[-1., 1.], [-1., 1.]])


def test_operation_gradient_widens_float16_samples(tmp_path, monkeypatch):
    records = [{"x": np.array([-60000, 0, 60000], dtype=np.float16),
                "signal": np.array([-60000, 60000, -60000], dtype=np.float16)}]
    with array_plot(tmp_path, monkeypatch, records) as (_window, plot):
        operation_option(plot, "dy/dx").input.setChecked(True)
        apply_operations(plot)
        np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], [2., 0., -2.])


def test_axis_swap_renders_exact_mixed_integer_coordinates(tmp_path, monkeypatch):
    expected = [2**63 + 1, 2**63 + 3, -1, -3]
    records = [
        {"x": np.arange(2), "signal": np.array(expected[:2], dtype=np.uint64)},
        {"x": np.arange(2, 4), "signal": np.array(expected[2:], dtype=np.int64)},
    ]
    with array_plot(tmp_path, monkeypatch, records) as (_window, plot):
        prior = plot.worker
        plot.axis_dropdown["x"].setCurrentText("signal")
        wait_for(lambda: plot.worker is not prior and not plot.worker.running)
        plot.monitor.stop()
        assert plot.line.getOriginalDataset()[0].tolist() == expected
        np.testing.assert_array_equal(plot.line.getData()[0], np.array(expected, dtype=float))
        assert plot.line.getData()[0].dtype.kind == "f"
        dialog = _open_csv_dialog(plot)
        rows = _export_from_dialog(monkeypatch, plot, dialog, tmp_path / "swapped.csv", ",")
        assert [int(row[0]) for row in rows[1:]] == expected


def test_mixed_integer_coordinates_fft_keeps_exact_spacing(tmp_path, monkeypatch):
    x = [2**63 - 3, 2**63 - 1, 2**63 + 1, 2**63 + 3]
    records = [
        {"x": np.array(x[:2], dtype=np.int64), "signal": np.array([1., 0.])},
        {"x": np.array(x[2:], dtype=np.uint64), "signal": np.array([-1., 0.])},
    ]
    with array_plot(tmp_path, monkeypatch, records) as (_window, plot):
        click_control(plot, "fftCheck")
        assert plot.plot.ctrl.fftCheck.isChecked()
        for refresh in (False, True):
            if refresh:
                plot.refreshWindow(force=True)
                wait_for(lambda: not plot.worker.running)
                plot.monitor.stop()
            assert plot.line.getOriginalDataset()[0].tolist() == x
            np.testing.assert_array_equal(plot.line.getData()[0], [0., .125, .25])
            np.testing.assert_array_equal(plot.line.getData()[1], [0., .5, 0.])
        click_control(plot, "fftCheck")
        assert plot.line.getOriginalDataset()[0].tolist() == x
