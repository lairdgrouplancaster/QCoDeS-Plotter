"""Shared fixtures and assertions for plot csv precision."""

import csv
from contextlib import closing

import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw
from pyqtgraph.exporters import CSVExporter
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect
from qcodes.parameters import ManualParameter

from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.support.windows.plot_integration import (
    configure_temp_qplot,
    database_artifact_state,
    wait_for,
)

__all__ = [
    '_create_measurements',
    '_export_from_dialog',
    '_open_csv_dialog',
    'precision_plot',
]


def _create_measurements(database_path, datasets):
    """Create only a new temporary database; close its writer before viewing."""
    initialise_or_create_database_at(str(database_path), journal_mode="DELETE")
    measurements = []
    with closing(connect(database_path)) as connection:
        experiment = load_or_create_experiment(
            "csv_precision", sample_name="sample", conn=connection,
        )
        x = ManualParameter("x")
        current = ManualParameter("current", unit="A")
        for x_values, y_values in datasets:
            # QCoDeS's scalar numeric converter passes through SQLite's
            # decimal text conversion. Array storage retains adjacent binary64
            # values at large offsets, so this case isolates export precision.
            array_storage = np.max(np.abs(x_values)) >= 1e6
            paramtype = "array" if array_storage else "numeric"
            measurement = Measurement(exp=experiment, name="precision")
            measurement.register_parameter(x, paramtype=paramtype)
            measurement.register_parameter(current, setpoints=(x,), paramtype=paramtype)
            with measurement.run(write_in_background=False) as datasaver:
                if array_storage:
                    datasaver.add_result((x, x_values), (current, y_values))
                else:
                    for x_value, y_value in zip(x_values, y_values, strict=True):
                        datasaver.add_result((x, float(x_value)), (current, float(y_value)))
            dataset = datasaver.dataset
            stored = dataset.get_parameter_data("current")["current"]
            expected = np.column_stack((stored["x"].ravel(), stored["current"].ravel()))
            # Exact comparisons, including subnormals, rather than tolerances
            # that would accept the original exporter silently returning zero.
            np.testing.assert_array_equal(expected, np.column_stack((x_values, y_values)))
            measurements.append((dataset.guid, expected))
    return measurements


@pytest.fixture
def precision_plot(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    database_path = tmp_path / "precision.db"
    datasets = request.param
    measurements = _create_measurements(database_path, datasets)
    source_before = database_artifact_state(database_path)
    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(lambda: not window._database_load_active)
        plots = []
        for guid, _expected in measurements:
            prior_count = len(window.windows)
            window.openPlot(guid=guid, show=False)
            wait_for(lambda prior_count=prior_count: len(window.windows) > prior_count)
            plot = window.windows[-1]
            wait_for(lambda plot=plot: hasattr(plot, "axis_data") and not plot.worker.running)
            plot.monitor.stop()
            plots.append(plot)
        target = plots[0]
        for source in plots[1:]:
            assert window.add_trace_to_plot(
                target, source._dataset_key, source.param.name, param=source.param,
            )
        wait_for(lambda: len(target.lines) == len(measurements))
        yield target, [expected for _guid, expected in measurements]
    finally:
        close_main_window(window)
        assert database_artifact_state(database_path) == source_before


def _open_csv_dialog(plot):
    plot.open_export_dialog()
    dialog = plot.widget.scene().exportDialog
    assert dialog.isVisible()
    for row in range(dialog.ui.formatList.count()):
        if dialog.ui.formatList.item(row).expClass is CSVExporter:
            dialog.ui.formatList.setCurrentRow(row)
            break
    assert type(dialog.currentExporter) is CSVExporter
    assert dialog.currentExporter.item is plot.plot
    assert dialog.ui.exportBtn.isEnabled()
    return dialog


def _export_from_dialog(monkeypatch, plot, dialog, target, delimiter):
    errors = []
    statuses = []
    monkeypatch.setattr(plot, "show_error", lambda *args: errors.append(args))
    monkeypatch.setattr(plot, "show_status", lambda *args: statuses.append(args))
    monkeypatch.setattr(
        qtw.QFileDialog, "getSaveFileName",
        lambda *_args, **_kwargs: (str(target), ""),
    )
    dialog.ui.exportBtn.click()
    tasks = tuple(plot._export_tasks)
    wait_for(lambda: all(task.done for task in tasks))
    assert all(task.error is None and not task.cancelled for task in tasks)
    assert errors == []
    assert statuses[-1][0] == f"Exported plot: {target}"
    assert not list(target.parent.glob(f".{target.name}.*"))
    with target.open(newline="", encoding="utf-8") as exported:
        return list(csv.reader(exported, delimiter=delimiter))
