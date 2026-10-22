"""Plot CSV retains extended QCoDeS array samples and ordinary float values."""

import csv

import numpy as np
import pytest
from pyqtgraph.exporters import CSVExporter
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.worker import loader
from qplot.windows._native_averaging import NativePlotItem
from qplot.windows._native_transforms import NativePlotDataItem
from qplot.windows._plot_export import PlotExportMixin
from tests.test_derivative_numeric_ranges import _source_state


@pytest.mark.parametrize("dtype", [np.float16, np.float32, np.float64, np.longdouble])
def test_plot_csv_preserves_float_samples_on_both_axes(tmp_path, qapplication, dtype):
    values = np.array(["0.1", "-0", "inf", "-inf", "nan"], dtype=dtype)
    plot = NativePlotItem()
    try:
        plot.addItem(NativePlotDataItem(values, values[::-1]))
        destination = tmp_path / "floats.csv"
        PlotExportMixin._write_line_csv_stage(None, str(destination), CSVExporter(plot))
        with destination.open(newline="", encoding="utf-8") as exported:
            rows = list(csv.reader(exported))[1:]
        # Ordinary float32 values must also retain their exact stored value when
        # imported as float64, rather than only round-tripping back to float32.
        imported_dtype = np.longdouble if dtype is np.longdouble else np.float64
        recovered = np.asarray(rows, dtype=imported_dtype)
        for column, expected in enumerate((values, values[::-1])):
            np.testing.assert_array_equal(recovered[:, column], expected.astype(imported_dtype))
            np.testing.assert_array_equal(
                np.signbit(recovered[:, column]), np.signbit(expected),
            )
    finally:
        plot.clear()


@pytest.mark.skipif(
    np.finfo(np.longdouble).nmant <= np.finfo(float).nmant,
    reason="This platform's longdouble has no additional recorded precision.",
)
def test_real_qcodes_extended_float_plot_csv_preserves_recorded_steps(tmp_path, qapplication):
    path = tmp_path / "extended.csv.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("extended CSV", sample_name="precision")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=("x",))
    signal = np.longdouble(1) + np.arange(3, dtype=np.longdouble) * np.finfo(np.longdouble).eps
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            saver.add_result(("x", np.arange(3.)), ("signal", signal))
            run_id = saver.run_id
    finally:
        experiment.conn.close()

    before = _source_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    plot = NativePlotItem()
    try:
        parameters = {parameter.name: parameter for parameter in dataset.get_parameters()}
        worker = loader(dataset.cache, parameters["signal"], parameters, {"x": "x"})
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert not errors and finished == [True]
        np.testing.assert_array_equal(worker.axis_data["y"], signal)
        assert np.unique(worker.axis_data["y"]).size == 3
        curve = NativePlotDataItem(worker.axis_data["x"], worker.axis_data["y"])
        plot.addItem(curve)
        np.testing.assert_array_equal(curve.getOriginalDataset()[1], signal)

        destination = tmp_path / "extended.csv"
        PlotExportMixin._write_line_csv_stage(None, str(destination), CSVExporter(plot))
        with destination.open(newline="", encoding="utf-8") as exported:
            rows = list(csv.reader(exported))
        recovered = np.asarray([row[1] for row in rows[1:]], dtype=np.longdouble)
        np.testing.assert_array_equal(recovered, signal)
        assert np.unique(recovered).size == 3
    finally:
        plot.clear()
        dataset.conn.close()
    assert _source_state(path) == before
