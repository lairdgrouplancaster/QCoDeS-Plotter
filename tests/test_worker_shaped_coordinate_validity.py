"""Shaped heatmaps must retain each recorded sample's coordinate validity."""

import hashlib
from functools import partial
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.qcodes_cache import update_cache_parameter_data
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.operation_registry import PLOT_OPERATION_SPECS, OperationCall
from qplot.tools.worker import PlotWorkCancelled, loader


def run_worker(worker):
    finished, errors = [], []
    worker.emitter.finished.connect(finished.append)
    worker.emitter.errorOccurred.connect(errors.append)
    worker.run()
    assert errors == []
    assert finished == [True]
    assert worker._sql_connection is None


@pytest.mark.parametrize("coordinate", ["fast", "slow"])
@pytest.mark.parametrize("nonfinite", [np.nan, np.inf, -np.inf], ids=["nan", "inf", "negative-inf"])
@pytest.mark.parametrize("swapped", [False, True], ids=["normal-axes", "swapped-axes"])
@pytest.mark.parametrize("row_mean", [False, True], ids=["raw", "subtract-row-mean"])
@pytest.mark.parametrize("incomplete", [False, True], ids=["complete", "incomplete"])
def test_real_qcodes_coordinate_validity_agrees_across_loading_paths(
    tmp_path, coordinate, nonfinite, swapped, row_mean, incomplete,
):
    path = tmp_path / "coordinate_validity.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("coordinate_validity", sample_name="test")
    source = {
        "slow": np.array([[0, 0, 0], [1, 1, 1]], dtype=float),
        "fast": np.array([[0, 1, 2], [0, 1, 2]], dtype=float),
        "signal": np.array([[0, 999, 2], [10, 11, 12]], dtype=float),
    }
    source[coordinate][0, 1] = nonfinite
    sample_count = 5 if incomplete else 6
    run_ids = []
    for shaped in (False, True):
        measurement = Measurement(exp=experiment)
        measurement.register_custom_parameter("slow")
        measurement.register_custom_parameter("fast")
        measurement.register_custom_parameter("signal", setpoints=("slow", "fast"))
        if shaped:
            measurement.set_shapes({"signal": (2, 3)})
        with measurement.run() as datasaver:
            for index in range(sample_count):
                datasaver.add_result(*(
                    (name, values.flat[index]) for name, values in source.items()
                ))
            run_ids.append(datasaver.dataset.run_id)

    before = hashlib.sha256(path.read_bytes()).digest()
    expected = np.array([[0, np.nan, 2], [10, 11, 12]], dtype=float)
    if incomplete:
        expected[1, 2] = np.nan
    if swapped:
        expected = expected.T
    raw_expected = expected.copy()
    if row_mean:
        expected = expected - np.nanmean(expected, axis=1, keepdims=True)
    spec = next(spec for spec in PLOT_OPERATION_SPECS["plot2d"]
                if spec.name == "Subtract Row Mean")
    operations = [OperationCall(spec.name, spec.func, cooperative=True)] if row_mean else []
    axes = {"x": "slow", "y": "fast"} if swapped else {"x": "fast", "y": "slow"}

    for shaped, run_id in zip((False, True), run_ids, strict=True):
        dataset = load_by_id_read_only(run_id, str(path))
        try:
            params = {param.name: param for param in dataset.get_parameters()}

            make_worker = partial(loader, dataset.cache, params["signal"], params, axes)

            def assert_result(worker, expected_grid=expected):
                np.testing.assert_array_equal(worker.axis_data["x"], [0, 1] if swapped else [0, 1, 2])
                np.testing.assert_array_equal(worker.axis_data["y"], [0, 1, 2] if swapped else [0, 1])
                np.testing.assert_array_equal(worker.dataGrid, expected_grid)

            # SQL detail reloads intentionally disallow operations. Compare
            # their raw grid, and run the operation on both ordinary loaders.
            sql_worker = make_worker(force_sql_heatmap=True, operations=[])
            run_worker(sql_worker)
            assert sql_worker.loaded_from_sql_heatmap
            assert_result(sql_worker, raw_expected)

            worker = make_worker(operations=operations)
            with patch.object(worker, "for_unshaped_2d", wraps=worker.for_unshaped_2d) as unshaped:
                run_worker(worker)
                if shaped:
                    # Missing coordinates must not disable the shaped fast path.
                    unshaped.assert_not_called()
            assert not worker.loaded_from_sql_heatmap
            assert_result(worker)
            cached_source = worker.cache_data["signal"]
            original = {name: values.copy() for name, values in cached_source.items()}
            for name, values in cached_source.items():
                np.testing.assert_array_equal(values.ravel()[:sample_count], source[name].ravel()[:sample_count])
            assert update_cache_parameter_data(
                dataset.cache, "signal", worker.updated_read_status,
                worker.updated_write_status, worker.cache_data,
            )
            cached_worker = make_worker(read_data=False, operations=operations)
            run_worker(cached_worker)
            assert_result(cached_worker)
            for name, values in cached_source.items():
                np.testing.assert_array_equal(values, original[name])
        finally:
            dataset.conn.close()

    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert not path.with_name(path.name + "-journal").exists()
    assert not path.with_name(path.name + "-wal").exists()


@pytest.mark.parametrize("phase", ["coordinate-mask", "grid-selection"])
def test_shaped_coordinate_masking_is_cancellable_without_modifying_source(phase):
    worker = loader.__new__(loader)
    worker.axes_dict = {"x": "fast", "y": "slow"}
    worker.param = SimpleNamespace(name="signal", depends_on_=("slow", "fast"))
    worker.param_dict = {name: SimpleNamespace(name=name) for name in ("slow", "fast")}
    data = {
        "slow": np.array([[0, 0, 0], [1, 1, 1]], dtype=float),
        "fast": np.array([[0, np.nan, 2], [0, 1, 2]], dtype=float),
        "signal": np.array([[0, 999, 2], [10, 11, 12]], dtype=float),
    }
    original = {name: values.copy() for name, values in data.items()}
    shaped_grid = worker._shaped_data_grid

    def cancel_during_grid(*args):
        calls = 0
        function = np.isfinite if phase == "coordinate-mask" else np.ix_

        def cancel_after_call(*values):
            nonlocal calls
            result = function(*values)
            calls += 1
            # The second isfinite checks the first original coordinate array.
            if calls == (2 if phase == "coordinate-mask" else 1):
                worker.cancel()
            return result

        with patch.object(np, "isfinite" if phase == "coordinate-mask" else "ix_",
                          side_effect=cancel_after_call):
            return shaped_grid(*args)

    with patch.object(worker, "_shaped_data_grid", side_effect=cancel_during_grid):
        with pytest.raises(PlotWorkCancelled):
            worker.for_shaped_2d(data, data["signal"])
    for name, values in data.items():
        np.testing.assert_array_equal(values, original[name])
