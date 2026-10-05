"""Public QCoDeS array records retain directional Boolean derivatives."""

import hashlib

import numpy as np
import pytest
from qcodes.dataset import Measurement, initialise_or_create_database_at, new_experiment

from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.worker import loader
from qplot.windows._native_transforms import NativePlotDataItem


@pytest.mark.parametrize("x,y", [
    (np.array([0., 1., 2.]), np.array([True, False, True])),
    (np.array([True, False]), np.array([4., 2.])),
    (np.array([True, False]), np.array([False, True])),
])
@pytest.mark.parametrize("mode", ["derivative", "phase"])
def test_native_boolean_derivative_from_qcodes_database(tmp_path, x, y, mode):
    database = tmp_path / "boolean.db"
    initialise_or_create_database_at(database)
    experiment = new_experiment("Boolean array oracle", "test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=("x",))
    try:
        with measurement.run() as saver:
            # DataSaver restricts array dtypes; the public DataSet API accepts
            # Boolean ndarrays and persists their dtype in QCoDeS' NPY cells.
            saver.dataset.add_results([{"x": x, "signal": y}])
        run_id = saver.run_id
    finally:
        experiment.conn.close()
    before = hashlib.sha256(database.read_bytes()).digest()
    dataset = load_by_id_read_only(run_id, database)
    try:
        specs = dataset.paramspecs
        worker = loader(dataset.cache, specs["signal"], specs, {"x": "x", "y": "signal"})
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert finished == [True] and errors == []
        curve = NativePlotDataItem(worker.axis_data["x"], worker.axis_data["y"])
        if mode == "derivative":
            curve.setDerivativeMode(True)
        else:
            curve.setPhasemapMode(True)
        observed_x, observed_y = curve.getData()
        expected_x = x[:-1] if mode == "derivative" else y[:-1]
        expected_y = np.diff(y.astype(float)) / np.diff(x.astype(float))
        np.testing.assert_array_equal(observed_x, expected_x)
        np.testing.assert_array_equal(observed_y, expected_y)
        original_x, original_y = curve.getOriginalDataset()
        np.testing.assert_array_equal(original_x, x)
        np.testing.assert_array_equal(original_y, y)
    finally:
        dataset.conn.close()
    assert hashlib.sha256(database.read_bytes()).digest() == before
