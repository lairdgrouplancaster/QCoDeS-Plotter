"""Compare complex slices, cut cursors and exported projections with the acquisition."""

import csv
import io
from itertools import product

import numpy as np
import pytest
from PyQt6 import QtCore, QtWidgets
from pyqtgraph.exporters import CSVExporter
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.complex_data import COMPLEX_ND_KEY
from tests.support.windows.plot3d import main as main
from tests.support.windows.plot_integration import wait_for
from tests.support.windows.svg_export_integration import select_exporter

DELAYS = (-2., 1., 6.)
BIASES = (-3., 4.)
FIELDS = (5., 8.)


def write_volume(path, precisions, reverse):
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("complex volume", "synthetic")
    expected = {}
    records = []
    try:
        measurement = Measurement(exp=experiment)
        for name, unit in (("field", "T"), ("bias", "V"), ("delay", "s")):
            measurement.register_custom_parameter(name, unit=unit, paramtype="array" if precisions else "numeric")
        measurement.register_custom_parameter("signal", unit="A", paramtype="array" if precisions else "complex",
                                              setpoints=("field", "bias", "delay"))
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            for field, bias in product(FIELDS, reversed(BIASES) if reverse else BIASES):
                delays = np.array(DELAYS[::-1] if bias < 0 else DELAYS)
                values = (10 * field + bias - 2 * delays) + 1j * (3 * bias - field + delays)
                if bias == BIASES[1]:
                    # Both components differ from their binary32 rounding at this cell.
                    values[-1] += (1 - 1j) * 2.**-28
                if precisions:
                    values = values.astype(precisions[BIASES.index(bias)])
                    saver.add_result(("field", np.full(delays.size, field)), ("bias", np.full(delays.size, bias)),
                                     ("delay", delays), ("signal", values))
                    records.append(values)
                else:
                    for delay, value in zip(delays, values, strict=True):
                        saver.add_result(("field", field), ("bias", bias), ("delay", delay), ("signal", value))
                expected.update(((field, bias, d), v) for d, v in zip(delays, values, strict=True))
        dataset = saver.dataset
        try:
            if records:
                blobs = dataset.conn.execute(f'SELECT CAST(signal AS BLOB) FROM "{dataset.table_name}" ORDER BY id')
                for (blob,), record in zip(blobs, records, strict=True):
                    stored = np.load(io.BytesIO(blob), allow_pickle=False)
                    np.testing.assert_array_equal(stored, record, strict=True)
            return dataset.guid, expected
        finally:
            dataset.conn.close()
    finally:
        experiment.conn.close()


def hover(plot, x, y):
    plot.mouseMoved(plot.vb.mapViewToScene(QtCore.QPointF(x, y)))


def cursor_value(plot, role):
    return complex(plot.pos_labels[role].text().replace(" ", "").replace("i", "j"))


def check_planes(plot, expected, mode):
    assert (plot.axis_param["x"].name, plot.axis_param["y"].name, plot._depth_name()) == ("delay", "bias", "field")
    for index, field in enumerate(FIELDS):
        plot.select_slice(index)
        wait_for(lambda field=field: plot._qplot_display_synchronized and plot.slice_frame.depth == field)
        np.testing.assert_array_equal(plot.axis_data["x"], DELAYS)
        np.testing.assert_array_equal(plot.axis_data["y"], BIASES)
        plane = np.array([[expected[field, bias, delay] for delay in DELAYS] for bias in BIASES])
        np.testing.assert_array_equal(plot.dataGrid, getattr(plane, mode))


def check_csv(cut, target, guid, coordinates, projected, monkeypatch):
    dialog = select_exporter(cut, CSVExporter)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(target), ""))
    dialog.ui.exportBtn.click()
    wait_for(lambda: target.exists() and not cut._export_tasks)
    dialog.close()
    with target.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    x_column, y_column = tuple(rows[0])[:2]
    assert {row["run_guid"] for row in rows} == {guid}
    np.testing.assert_array_equal([float(row[x_column]) for row in rows], coordinates)
    np.testing.assert_array_equal([float(row[y_column]) for row in rows], projected)


@pytest.mark.parametrize(("precisions", "reverse"), [
    (("complex64", "complex128"), False),
    (("complex64", "complex128"), True),
    (("complex64", "complex64"), False),
    (("complex128", "complex128"), False),
    (None, False),
], ids=["64-128", "128-64", "64-64", "128-128", "scalar"])
@pytest.mark.parametrize(("mode", "cut_action"), [("imag", "Horizontal Cut"), ("real", "Vertical Cut")])
def test_complex_volume_projection_cursor_and_export(main, tmp_path, monkeypatch, precisions, reverse, mode, cut_action):
    path = tmp_path / "acquisition.db"
    guid, expected = write_volume(path, precisions, reverse)
    window, errors = main
    window.config.update(COMPLEX_ND_KEY, mode)
    window.show()
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active and window._selected_run_guid == guid)
    window.plotRunButton.click()
    wait_for(lambda: len(window.windows) == 1)
    plot = window.windows[0]
    wait_for(lambda: plot._qplot_display_synchronized or errors)
    assert not errors
    check_planes(plot, expected, mode)

    # Both the parent and child cursors should identify the acquired sample.
    field, bias, delay = FIELDS[-1], BIASES[1], DELAYS[1]
    value = expected[field, bias, delay]
    hover(plot, delay, bias)
    assert tuple(float(plot.pos_labels[role].text()) for role in ("x", "y", "depth")) == (delay, bias, field)
    assert cursor_value(plot, "z") == value
    next(action for action in plot.vbMenu.actions() if action.text() == cut_action).trigger()
    wait_for(lambda: len(window.windows) == 2 and window.windows[-1]._qplot_display_synchronized)
    cut = window.windows[-1]
    horizontal = cut_action == "Horizontal Cut"
    coordinates = DELAYS if horizontal else BIASES
    values = [expected[field, bias, x] if horizontal else expected[field, x, delay] for x in coordinates]
    projected = getattr(np.asarray(values), mode)
    assert cut.axis_param["x"].name == ("delay" if horizontal else "bias")
    x_data, y_data = cut.line.getData()
    np.testing.assert_array_equal(x_data, coordinates)
    np.testing.assert_array_equal(y_data, projected)
    cut.force_all_axes_autoscale()
    coordinate = delay if horizontal else bias
    hover(cut, coordinate, getattr(value, mode))
    assert float(cut.pos_labels["x"].text()) == coordinate
    shown = cursor_value(cut, "y")

    check_csv(cut, tmp_path / "cut.csv", guid, coordinates, projected, monkeypatch)
    assert shown == value
