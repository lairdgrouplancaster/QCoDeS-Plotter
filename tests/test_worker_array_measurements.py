"""Real QCoDeS array records must retain their paired 1D sample order."""

import hashlib

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.qcodes_cache import update_cache_parameter_data
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.worker import loader


def _new_database(tmp_path):
    path = tmp_path / "arrays.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    return path, load_or_create_experiment("arrays", sample_name="test")


def _run_worker(dataset, parameter_name, x_name, *, read_data):
    params = {param.name: param for param in dataset.get_parameters()}
    worker = loader(
        dataset.cache,
        params[parameter_name],
        params,
        {"x": x_name},
        read_data=read_data,
    )
    finished = []
    errors = []
    worker.emitter.finished.connect(finished.append)
    worker.emitter.errorOccurred.connect(errors.append)
    worker.run()
    assert errors == []
    assert finished == [True]
    return worker


@pytest.mark.parametrize(
    "records",
    [
        [([0, 1, 2], [2, 0, 1])],
        [([0, 1, 2], [1, 1, 1])],
        [([0, 1, 2], [2, 0, 1]), ([3, 4, 5], [1, 1, 0])],
        [([0, 1, 2], [2, np.nan, 1]), ([3, np.nan, 5], [np.nan, 4, 0])],
        [([0, 2, 1], [0, 2, 1]), ([3, 2, 1, 0], [13, 12, 11, 10])],
        [([0, 2, 1], [0, np.nan, 1]),
         ([3, 2, np.nan, 0], [13, 12, 11, 10])],
    ],
    ids=["nonmonotonic", "constant", "multiple-records", "nans",
         "variable-length", "variable-length-nans"],
)
def test_array_measurement_keeps_paired_samples_on_initial_and_cached_load(
    tmp_path, records
):
    path, experiment = _new_database(tmp_path)
    measurement = Measurement(exp=experiment, name="array_line")
    measurement.register_custom_parameter("x", paramtype="array")
    measurement.register_custom_parameter(
        "signal", setpoints=("x",), paramtype="array"
    )
    with measurement.run() as datasaver:
        for x_values, y_values in records:
            datasaver.add_result(
                ("x", np.asarray(x_values, dtype=float)),
                ("signal", np.asarray(y_values, dtype=float)),
            )
        run_id = datasaver.dataset.run_id

    # The viewer uses a read-only dataset. Neither worker pass may change the
    # QCoDeS main database or its rollback journal.
    before = hashlib.sha256(path.read_bytes()).digest()
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        x_expected = np.concatenate([np.asarray(x) for x, _ in records])
        y_expected = np.concatenate([np.asarray(y) for _, y in records])
        valid = ~np.isnan(x_expected) & ~np.isnan(y_expected)
        for x_name in ("x", "signal"):
            initial = _run_worker(dataset, "signal", x_name, read_data=True)
            source = initial.cache_data["signal"]
            ragged = len({len(x) for x, _ in records}) > 1
            assert source["signal"].ndim == (1 if ragged else 2)
            assert (source["signal"].dtype == object) == ragged
            original = {name: [record.copy() for record in values]
                        for name, values in source.items()}
            assert not hasattr(initial, "dataGrid")
            expected_x = x_expected[valid] if x_name == "x" else y_expected[valid]
            expected_y = y_expected[valid] if x_name == "x" else x_expected[valid]
            np.testing.assert_array_equal(initial.axis_data["x"], expected_x)
            np.testing.assert_array_equal(initial.axis_data["y"], expected_y)
            assert initial.axis_param["x"].name == x_name

            assert update_cache_parameter_data(
                dataset.cache,
                "signal",
                initial.updated_read_status,
                initial.updated_write_status,
                initial.cache_data,
            )
            cached = _run_worker(dataset, "signal", x_name, read_data=False)
            assert not hasattr(cached, "dataGrid")
            np.testing.assert_array_equal(cached.axis_data["x"], expected_x)
            np.testing.assert_array_equal(cached.axis_data["y"], expected_y)
            for name, values in source.items():
                for value, expected in zip(values, original[name], strict=True):
                    np.testing.assert_array_equal(value, expected)
    finally:
        dataset.conn.close()
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_variable_length_signal_broadcasts_scalar_1d_setpoint(tmp_path):
    path, experiment = _new_database(tmp_path)
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x")
    measurement.register_custom_parameter("signal", setpoints=("x",), paramtype="array")
    with measurement.run() as datasaver:
        datasaver.add_result(("x", 2), ("signal", np.array([0., np.nan, 1.])))
        datasaver.add_result(("x", 1), ("signal", np.array([13., 12., 11., 10.])))
        run_id = datasaver.dataset.run_id
    before = hashlib.sha256(path.read_bytes()).digest()
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        for x_name in ("x", "signal"):
            worker = _run_worker(dataset, "signal", x_name, read_data=True)
            expected = {"x": [2, 2, 1, 1, 1, 1], "signal": [0, 1, 13, 12, 11, 10]}
            np.testing.assert_array_equal(worker.axis_data["x"], expected[x_name])
            np.testing.assert_array_equal(
                worker.axis_data["y"], expected["signal" if x_name == "x" else "x"],
            )
    finally:
        dataset.conn.close()
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_two_dependency_array_measurement_still_loads_as_heatmap(tmp_path):
    path, experiment = _new_database(tmp_path)
    measurement = Measurement(exp=experiment, name="array_map")
    measurement.register_custom_parameter("x", paramtype="array")
    measurement.register_custom_parameter("y", paramtype="array")
    measurement.register_custom_parameter(
        "signal", setpoints=("y", "x"), paramtype="array"
    )
    with measurement.run() as datasaver:
        for row in range(2):
            datasaver.add_result(
                ("x", np.array([0.0, 1.0, 2.0])),
                ("y", np.full(3, float(row))),
                ("signal", np.array([10.0, 11.0, 12.0]) + row * 10),
            )
        run_id = datasaver.dataset.run_id

    dataset = load_by_id_read_only(run_id, str(path))
    try:
        params = {param.name: param for param in dataset.get_parameters()}
        worker = loader(
            dataset.cache,
            params["signal"],
            params,
            {"x": "x", "y": "y"},
        )
        finished = []
        errors = []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert errors == []
        assert finished == [True]
        np.testing.assert_array_equal(worker.axis_data["x"], [0, 1, 2])
        np.testing.assert_array_equal(worker.axis_data["y"], [0, 1])
        np.testing.assert_array_equal(worker.dataGrid, [[10, 11, 12], [20, 21, 22]])
    finally:
        dataset.conn.close()
