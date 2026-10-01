"""Numeric fidelity through the real Export Plot dialog and QCoDeS data."""

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
from tests.windows.test_plot_integration import (
    configure_temp_qplot,
    database_artifact_state,
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
            window.openPlot(guid=guid, show=False)
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
    assert errors == []
    assert statuses[-1][0] == f"Exported plot: {target}"
    assert not list(target.parent.glob(f".{target.name}.*"))
    with target.open(newline="", encoding="utf-8") as exported:
        return list(csv.reader(exported, delimiter=delimiter))


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
